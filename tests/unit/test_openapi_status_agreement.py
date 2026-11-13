"""The published status codes must be ones the routes can actually return.

``tests/unit/test_openapi_drift.py`` is a strong gate, but BOTH sides of its
comparison are derived from the same decorators via ``app.openapi()``. The
handler body is never consulted. So a route that declares ``201`` and returns
``200`` produces two identical documents and the drift gate stays green — which
is exactly what it was doing when this file was written, on four routes.

The mechanism, in the installed FastAPI (``fastapi/routing.py``)::

    raw_response = await run_endpoint_function(...)
    if isinstance(raw_response, Response):
        response = raw_response            # <- returned VERBATIM
    else:
        response_args = _build_response_args(...)   # <- status_code applied here
        content = await serialize_response(...)     # <- response_model applied here

When a handler returns a ``Response`` instance, both the declared ``status_code``
and ``response_model`` are skipped. The decorator's ``status_code`` still sets the
OpenAPI response KEY, but never the wire status. That asymmetry is the defect
class this gate exists for.

WHAT THIS GATE IS NOT. It does not assert that the routes are right — they are.
Every dual-status contract here is deliberate ("idempotent — returns existing app
if key matches") and is pinned by ``tests/unit/test_tenants.py`` and
``tests/unit/test_concurrent_create_races.py``. It asserts that the DOCUMENT says
so, because the document is what license-authz, fr-backend, admin_app and the
n8n custom nodes generate their clients from.

Design notes — the ways a gate like this normally fails to be a gate:

* It drives the routes for real and reads the OBSERVED status, rather than
  re-deriving an expectation from the same decorators it is checking.
* It has a VACUITY FLOOR on the number of (route, arm) pairs actually driven, so
  a selector that matches nothing fails instead of passing.
* Its positive control is measured BEFORE it is relied on: a control naming a
  string the fix has just deleted can never hold, which this fleet has shipped
  before.
* It asserts each arm reached a DISTINCT status, so a fixture that quietly made
  both arms take the same branch cannot look like coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = ROOT / "docs" / "openapi.json"

#: Routes whose handler returns a raw ``JSONResponse`` on more than one arm, i.e.
#: every route where the declared status can silently disagree with the wire.
#: Each entry drives the CREATE arm and then the IDEMPOTENT arm of the same call.
DUAL_STATUS_ROUTES = [
    pytest.param("POST", "/tenants", "create_tenant", id="post-tenants"),
    pytest.param("POST", "/apps", "create_app", id="post-apps"),
    pytest.param("POST", "/regions", "create_region", id="post-regions"),
]

#: Minimum (route, arm) pairs this module must actually drive. Not a ">=0" floor
#: that a broken selector satisfies trivially: 3 routes x 2 arms, plus the two
#: PUT /tenants arms.
MINIMUM_DRIVEN_PAIRS = 8


def _bodies(method: str, path: str) -> dict[str, Any]:
    """A request body that will succeed twice for the same identity."""
    if path == "/tenants":
        # Migration 050: POST /tenants is idempotent on (owner, external_ref), no
        # longer on the name.
        return {"name": "status-agreement-tenant", "external_ref": "status-agreement-ref"}
    if path == "/apps":
        return {"key": "status-agreement-app", "name": "Status Agreement"}
    if path == "/regions":
        return {"code": "sa", "name": "Status Agreement Region"}
    raise AssertionError(f"no body registered for {method} {path}")


def _declared_codes(operation_id: str) -> set[int]:
    """Status codes the COMMITTED spec publishes for this operation."""
    spec = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    for _path, methods in spec["paths"].items():
        for _method, operation in methods.items():
            if operation.get("operationId") == operation_id:
                return {int(code) for code in operation["responses"] if code.isdigit()}
    raise AssertionError(
        f"operationId {operation_id!r} is not in {SNAPSHOT_PATH} — either the route "
        "was renamed or the snapshot was not regenerated"
    )


class TestDeclaredStatusCodesMatchObservedOnes:
    """Drive both arms of every dual-status route and compare to the spec."""

    @pytest.mark.parametrize("method,path,operation_id", DUAL_STATUS_ROUTES)
    def test_both_arms_are_declared(
        self, auth_client_admin: TestClient, method: str, path: str, operation_id: str
    ):
        body = _bodies(method, path)

        created = auth_client_admin.request(method, path, json=body)
        idempotent = auth_client_admin.request(method, path, json=body)

        observed = {created.status_code, idempotent.status_code}

        # The two arms must be DIFFERENT, or this route was not exercised the way
        # the test claims and "both declared" would be a one-arm result wearing a
        # two-arm name.
        assert len(observed) == 2, (
            f"{method} {path} returned {observed} for create AND repeat — the "
            "idempotent arm was never reached, so this proves nothing"
        )
        assert observed == {200, 201}, f"unexpected statuses for {method} {path}: {observed}"

        declared = _declared_codes(operation_id)
        missing = observed - declared
        assert not missing, (
            f"{method} {path} can return {sorted(missing)} but the published spec "
            f"declares only {sorted(declared)}. Clients are generated from that "
            f"document (orval), so an undeclared success status is a response no "
            f"generated client is typed to handle. Fix the decorator's "
            f"`responses=`, then re-run `uv run python scripts/export-openapi.py`."
        )

    def test_put_tenants_declares_both_arms(self, auth_client_admin: TestClient):
        """``PUT /tenants/{tenant_id}`` — a caller-supplied id, so it needs its own
        driver rather than the parametrised body table."""
        tenant_id = "01000000-0000-7000-8000-00000000abcd"
        body = {"name": "status-agreement-put"}

        created = auth_client_admin.put(f"/tenants/{tenant_id}", json=body)
        idempotent = auth_client_admin.put(f"/tenants/{tenant_id}", json=body)

        observed = {created.status_code, idempotent.status_code}
        assert len(observed) == 2, (
            f"PUT /tenants/{{id}} returned {observed} twice — the idempotent arm was never reached"
        )
        assert observed == {200, 201}

        declared = _declared_codes("register_tenant")
        missing = observed - declared
        assert not missing, (
            f"PUT /tenants/{{tenant_id}} can return {sorted(missing)} but the spec "
            f"declares only {sorted(declared)}"
        )


class TestGateIsNotVacuous:
    """The floors, so a selector that matches nothing cannot pass as coverage."""

    def test_it_drives_at_least_the_known_pairs(self):
        pairs = len(DUAL_STATUS_ROUTES) * 2 + 2  # +2 for the PUT /tenants arms
        assert pairs >= MINIMUM_DRIVEN_PAIRS, (
            f"only {pairs} (route, arm) pairs are driven; emptying "
            "DUAL_STATUS_ROUTES would make every assertion above trivially true"
        )

    def test_the_snapshot_lookup_reads_the_snapshot(self):
        """POSITIVE CONTROL — and it has to DISCRIMINATE, not merely be true.

        ``_declared_codes`` is the only thing standing between this module and a
        permanent green: if it returned a fixed superset, every ``missing`` set
        would be empty and nothing here would ever fail again.

        Asserting only what IS declared cannot catch that — a superset contains it
        too. Measured, not theorised: a mutant returning
        ``{200, 201, 400, 401, 403, 404, 422, 500}`` for every operation SURVIVED
        the first version of this test, which asserted exactly that. So the
        assertions below are ABSENCES, read off the snapshot as it stands:
        ``list_apps`` is a plain GET and declares neither ``201`` nor ``422``.
        A hardcoded superset fails both.
        """
        list_apps = _declared_codes("list_apps")

        assert 200 in list_apps, "the lookup is not finding the operation at all"
        assert 201 not in list_apps, (
            "list_apps declares a 201 it cannot return — or _declared_codes stopped "
            "reading the snapshot and is answering from a constant"
        )
        assert 422 not in list_apps, (
            "same: a GET with no request body has no validation-error arm, so a 422 "
            "here means the lookup is not reading the document"
        )
        # And it must distinguish two operations on the SAME path, i.e. key on
        # operationId rather than returning the first entry it walks past.
        assert 201 in _declared_codes("create_app")

    def test_the_snapshot_lookup_fails_loudly_on_an_unknown_operation(self):
        """The other half of the control: silence on a miss would let a renamed
        route slip through as "nothing missing"."""
        with pytest.raises(AssertionError, match="not in"):
            _declared_codes("no_such_operation_id")
