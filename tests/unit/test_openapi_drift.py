"""Fail CI when the live OpenAPI spec diverges from ``docs/openapi.json``.

Consumers (license-authz, fr-backend, admin_app, the n8n custom nodes) generate
clients from the committed snapshot, so a route that exists only in the code is
a route nobody outside this repo can call. That is not hypothetical: at the time
this gate was written the snapshot was missing 16 operations, including
``PUT /tenants/{tenant_id}`` — the endpoint used to register a real client's
tenant.

Design notes, i.e. the ways a gate like this normally fails to be a gate:

* It **cannot skip itself green.** A missing snapshot is ``pytest.fail``, never
  ``pytest.skip``; there is no ``importorskip`` and no ``integration`` marker
  (``pyproject`` addopts deselect ``-m 'not integration'``), so it runs in every
  ``pytest tests/`` invocation the CI archetype makes.
* It **compares deeply**, not path-set-only — see
  ``TestGateDiscriminatingPower``, which mutates only an ``operationId`` and
  only a schema property ``type`` and requires both to be reported.
* It has a **vacuity floor** (``TestGateIsNotVacuous``): an over-eager
  ``strip_volatile`` or a truncated snapshot cannot make the comparison
  trivially true.
* The live spec is produced by running ``scripts/export-openapi.py`` itself, in
  a subprocess, under the shared environment block from
  ``scripts/openapi_env.py`` — so the gate cannot pass against a document the
  documented fix command would not reproduce, and it does not depend on the
  ambient environment of the pytest process.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.openapi_env import export_env
from tests.support.openapi_drift import drift_report, strip_volatile

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = ROOT / "docs" / "openapi.json"
EXPORTER = ROOT / "scripts" / "export-openapi.py"

REGENERATE = "uv run python scripts/export-openapi.py"

# Vacuity backstop. These are EXACT pins, not ">=" floors: a floor cannot catch
# the drop it exists for (set it to 0 and everything stays green), and it also
# cannot catch a route being added without a snapshot regeneration. Exact pins
# are self-guarding — zeroing either constant fails immediately — at the price of
# one deliberate line edit whenever the surface really does change, which is
# exactly the moment a human should be looking.
# 29 -> 30: /tenants/{name_or_id}/archive is a new PATH.
# 43 -> 45: rename_tenant (PATCH on the EXISTING /tenants/{name_or_id} path, so
#           it adds an operation without adding a path) + set_tenant_archived.
# 30 -> 31 and 45 -> 46: list_tenant_offerings, GET /tenants/{name_or_id}/offerings —
#           a new path AND a new operation (migration 025 recorded the offering
#           that produced each per-product posture; this reads it back).
# 46 -> 47: get_tenant_app_byo_mode, GET /tenants/{name_or_id}/apps/{app_key}/byo-mode.
#           A new OPERATION on an EXISTING path (PUT and DELETE were already
#           there), which is why EXPECTED_PATHS does not move — the read that
#           completes the per-app set.
# 31 -> 32 and 47 -> 48: internal_list_tenants, GET /internal/tenants — a new path
#           AND a new operation. billing resolves a tenant uuid to its human name
#           through it when rendering the cost ledger.
# 32 -> 33 and 48 -> 49: internal_get_subject, GET /internal/subjects/{subject_id}
#           — a new path AND a new operation. It is the READER for migration 029's
#           `subject.tenant_id`: nothing else in this API exposes a `subject` row,
#           so without it the new column is unreadable outside the database.
# 33 -> 34 and 49 -> 50: set_tenant_cost_centre, PATCH /tenants/{name_or_id}/cost-centre
#           — a new path AND a new operation. It is the WRITER for migration 034's
#           `ck_tenants_cost_centre` vocabulary: before it, `cost_centre` was
#           insert-only and the four NULL rows had no HTTP path to be filled.
# 34 -> 35 and 50 -> 51: internal_tenant_seat_count,
#           GET /internal/tenants/{tenant_id}/seat-count — a new path AND a new
#           operation. It is the DENOMINATOR for billing's per-seat access charge
#           ($25/seat, George 2026-09-10). It exists here rather than in billing
#           because the seat predicate needs tenant `kind` and `archived_at`,
#           which /internal/licenses does not carry: 164 rows are status='active'
#           and only 8 belong to customer tenants, so counting that list in
#           billing bills ~156 phantom seats (~$3,900/month of invented revenue).
# 35 -> 36 and 51 -> 52: create_operator_token,
#           POST /licenses/{license_id}/operator-token — a new path AND a new
#           operation. The one door by which a licence token can carry a role
#           (`platform-admin`, short-lived, own licence only), which ingestion's
#           operator purge reads off /license/introspect's new `roles` field.
# 36 -> 37 and 52 -> 53: internal_mint_tenant_bundle_token,
#           POST /internal/tenants/{tenant_id}/bundle-token — a new path AND a new
#           operation. The server-side bundle resolver (TENANCY_AND_IMPROVEMENT.md
#           §5): mints a <=60-min token on the tenant's active bundle licence.
# 37 -> 39 and 53 -> 55 (tenant-inference-provisioning PLAN §2.2): two new paths,
#           two new operations — internal_ensure_tenant_bundle,
#           POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure, and
#           set_tenant_budget_currency, PUT /tenants/{name_or_id}/budget-currency.
# 40 -> 42 and 56 -> 59 (migration 050, tenant ownership per app): two new paths,
#           three new operations — put_tenant_grant / delete_tenant_grant on
#           /tenants/{name_or_id}/grants/{owner_key} and list_tenant_grants on
#           /tenants/{name_or_id}/grants.
# 42 -> 43 and 59 -> 60 (2026-10-31): set_license_budget,
#           PATCH /licenses/{license_id}/budget — a new path AND a new operation.
#           The licence-level WRITER for token_budget_override, which was
#           insert-only: editing billing's private copy is reverted by
#           sync_from_licenses, so this route is the durable lever.
EXPECTED_PATHS = 43
EXPECTED_OPERATIONS = 60
KNOWN_OPERATION_ID = "revoke_token"

#: Operations that publish no ``200 application/json`` body at all, because their
#: success arm is 201 or 204. Pinned as an exact SET rather than skipped: an
#: operation that quietly STOPS publishing a 200 body would otherwise just be one
#: fewer thing for the untyped-schema scan below to look at.
OPERATIONS_WITHOUT_A_JSON_200 = frozenset(
    {
        "create_license_token",  # 201
        "create_operator_token",  # 201 (OperatorTokenResponse)
        "create_plan",  # 201
        "create_user_license",  # 201
        "delete_app",  # 204
        "delete_plan",  # 204
        "delete_tenant_grant",  # 204 (migration 050)
        "internal_mint_tenant_bundle_token",  # 201 (BundleTokenResponse)
    }
)
#: EXPECTED_OPERATIONS minus the seven above. Exact, for the same reason the two
#: constants above it are exact.
# 38 -> 40: both new tenant operations answer 200 through TenantResponse.
# 40 -> 41: list_tenant_offerings answers 200 through list[TenantOfferingRecord].
# 41 -> 42: get_tenant_app_byo_mode answers 200 through TenantOfferingRecord.
# 42 -> 43: internal_list_tenants answers 200 through list[InternalTenantResponse].
# 43 -> 44: internal_get_subject answers 200 through InternalSubjectResponse.
# 44 -> 45: set_tenant_cost_centre answers 200 through TenantResponse.
# 45 -> 46: internal_tenant_seat_count returns a typed InternalSeatCountResponse
# (seats + subjects + basis + tenant_kind/archived + as_of), so the scan sees one
# more typed 200 body. A bare dict here would have been the easy shortcut and is
# exactly what this counter exists to refuse.
# 46 -> 48: set_tenant_budget_currency answers 200 through TenantResponse, and
# internal_ensure_tenant_bundle through BundleEnsureResponse (PLAN §2.2).
# 49 -> 51: put_tenant_grant (its idempotent arm) and list_tenant_grants answer 200
# through TenantGrantResponse / list[TenantGrantResponse]; delete_tenant_grant is 204.
# 51 -> 52: set_license_budget answers 200 through LicenseResponse.
EXPECTED_TYPED_JSON_200_OPERATIONS = 52

#: Starlette/FastAPI response classes. Returning ANY of them from a handler makes
#: FastAPI hand it back verbatim, skipping ``response_model`` — see
#: ``TestSchemasAreEnforcedNotJustPublished``.
_RESPONSE_CLASSES = frozenset(
    {
        "Response",
        "JSONResponse",
        "ORJSONResponse",
        "UJSONResponse",
        "HTMLResponse",
        "PlainTextResponse",
        "RedirectResponse",
        "StreamingResponse",
        "FileResponse",
    }
)

#: The ten operations that published a bare ``{}`` until this was fixed. Their
#: handlers each have a single 200 arm, so each returns the plain dict/model and
#: FastAPI serialises it THROUGH the declared schema. Listed explicitly rather
#: than derived, so re-introducing a raw ``Response`` on one of them is a named
#: failure instead of a silent loss of enforcement.
OPERATIONS_WHOSE_SCHEMA_IS_ENFORCED = frozenset(
    {
        "clear_tenant_app_byo_mode",
        "create_offering",
        "delete_offering",
        "get_offering",
        "get_tenant_byo_mode",
        "list_offerings",
        "provision_tenant_offering",
        "set_tenant_app_byo_mode",
        "set_tenant_byo_mode",
        "unprovision_tenant_offering",
    }
)

#: Operations that legitimately return a raw ``Response``: each answers 200 OR 201
#: depending on whether the row already existed, and only a returned Response can
#: pick the status per-arm. Their ``response_model`` is documentation, not a check
#: — which is precisely why they are the positive control below.
OPERATIONS_WITH_A_DELIBERATE_RAW_RESPONSE = frozenset(
    {"create_app", "create_region", "create_tenant", "register_tenant"}
)

# Routes deliberately absent from the published schema. Pinned as a SET so a new
# `include_in_schema=False` route trips this test: a route born hidden is
# invisible to the drift comparison (it is in neither document), whereas HIDING
# an already-published route is caught, because it disappears from the export.
HIDDEN_ROUTES = frozenset(
    {
        "/",  # root redirect to the docs UI, not an API operation
        "/licenses/health",  # alias of /health, hidden to keep one health entry in the docs
        # Prometheus scrape endpoint. Deliberately hidden: it is an operational
        # surface for the `kubernetes-pods` scrape job, not part of the service's
        # API contract, and its body is the text exposition format rather than
        # JSON, so publishing it would put a non-conforming response in the schema.
        "/metrics",
    }
)
# FastAPI's own Swagger/ReDoc endpoints. They are hidden by the framework, exist
# only when DOCS_ENABLED, and are not part of the service's API contract — so
# they are excluded rather than pinned, which would make this test depend on
# whether docs happen to be enabled in the running environment.
FRAMEWORK_DOC_ROUTES = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})


def _operation_ids(spec: dict[str, Any]) -> set[str]:
    return {
        operation["operationId"]
        for path_item in spec.get("paths", {}).values()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS and isinstance(operation, dict) and "operationId" in operation
    }


def _operation_count(spec: dict[str, Any]) -> int:
    return sum(
        1
        for path_item in spec.get("paths", {}).values()
        for method in path_item
        if method in _HTTP_METHODS
    )


def _json_200_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Map operationId -> the schema published for its ``200 application/json`` body.

    Operations with no such body (201/204 arms) are simply absent; that set is
    pinned in ``OPERATIONS_WITHOUT_A_JSON_200``.
    """
    schemas: dict[str, Any] = {}
    for path_item in spec.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            content = operation.get("responses", {}).get("200", {}).get("content", {})
            body = content.get("application/json")
            if isinstance(body, dict) and "schema" in body:
                schemas[operation["operationId"]] = body["schema"]
    return schemas


def _handler_returns_a_raw_response(operation_id: str) -> bool:
    """True if the handler behind ``operation_id`` has a ``return <SomeResponse>(...)``.

    Parsed with ``ast``, NOT substring-matched: the handlers this branch fixed each
    carry a comment saying "Plain dict, NOT JSONResponse", so a ``"JSONResponse" in
    source`` check would report every one of them as still broken.
    """
    import main

    for route in main.app.routes:
        # getattr for both: Starlette's BaseRoute declares neither attribute —
        # Mount and WebSocketRoute legitimately lack them.
        if getattr(route, "operation_id", None) != operation_id:
            continue
        endpoint = getattr(route, "endpoint", None)
        assert endpoint is not None, f"route for {operation_id!r} has no endpoint"
        tree = ast.parse(inspect.getsource(endpoint))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _RESPONSE_CLASSES:
                return True
        return False
    raise AssertionError(f"no route publishes operationId {operation_id!r}")


def _run_exporter(out: Path, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Run scripts/export-openapi.py in a subprocess and return the document it wrote."""
    env = export_env()
    if overrides:
        env.update(overrides)
    result = subprocess.run(
        [sys.executable, str(EXPORTER), "--out", str(out)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0 or not out.exists():
        pytest.fail(
            f"`{EXPORTER.name} --out ...` exited {result.returncode} — the OpenAPI export is "
            f"broken, so drift cannot be assessed.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return json.loads(out.read_text())


@pytest.fixture(scope="module")
def live_spec(tmp_path_factory) -> dict[str, Any]:
    """The spec the documented regeneration command would write, right now."""
    return _run_exporter(tmp_path_factory.mktemp("openapi") / "live.json")


# A tenant id no code path can produce on its own, used to prove no environment
# value reaches the published document. It must differ from every value the
# service or its deployment could supply, or the test agrees by coincidence and
# silently retires itself — a mutation run caught exactly that when this was set
# to main.py's own fallback. The constraint is asserted, not just commented
# (see test_foreign_tenant_id_is_actually_foreign).
FOREIGN_TENANT_ID = "0195f48b-d000-7000-8000-00000000c0de"
#: Values the running service could legitimately emit. FOREIGN_TENANT_ID must be none of them.
NON_FOREIGN_TENANT_IDS = frozenset(
    {
        "n8n-dev",  # the value the snapshot used to publish
        "0195f48b-d000-7000-8000-000000000001",  # main.py's old fallback / the prod Deployment value
    }
)


@pytest.fixture(scope="module")
def foreign_env_spec(tmp_path_factory) -> dict[str, Any]:
    """The same code exported by a maintainer whose environment differs from ours."""
    return _run_exporter(
        tmp_path_factory.mktemp("openapi-foreign") / "live.json",
        {
            "DEFAULT_TENANT_ID": FOREIGN_TENANT_ID,
            "SERVICE_NAME": "licensing-service-local",
            "VERSION": "9.9.9-someone-elses-build",
        },
    )


def _export_without_the_pin(overrides: dict[str, str]) -> dict[str, Any]:
    """Build the spec deliberately BYPASSING scripts/openapi_env's pinning.

    Not a straw man: this is how the committed snapshot came to document
    ``X-Tenant-Id: n8n-dev`` while ``main.py``'s own fallback was a UUID — it was
    exported by tooling that took the value from the ambient environment. Two
    such documents must still agree.
    """
    script = (
        "import json,sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from main import app;"
        "sys.stdout.write(json.dumps(app.openapi(), sort_keys=True))"
    )
    env = export_env()
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"unpinned export failed ({result.returncode}):\n{result.stderr}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def snapshot_spec() -> dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        pytest.fail(f"{SNAPSHOT_PATH} not found — run `{REGENERATE}` and commit the result.")
    return json.loads(SNAPSHOT_PATH.read_text())


def test_openapi_matches_committed_snapshot(snapshot_spec, live_spec):
    """docs/openapi.json must equal what the exporter produces from the code."""
    report = drift_report(snapshot_spec, live_spec)
    if report:
        pytest.fail(
            "\n".join(
                [
                    "OpenAPI drift detected vs docs/openapi.json.",
                    *report,
                    "",
                    f"Fix: `{REGENERATE}` and commit the updated docs/openapi.json.",
                ]
            )
        )


class TestGateIsNotVacuous:
    """Guard the guard: the comparison above must be over real content."""

    @staticmethod
    def _assert_surface(spec: dict[str, Any], label: str) -> None:
        # Bind to plain ints/bools first: `assert len(x.get(...)) == N` makes
        # pytest dump the entire 178 KB spec into the failure report.
        paths = len(spec.get("paths", {}))
        operations = _operation_count(spec)
        has_known_id = KNOWN_OPERATION_ID in _operation_ids(spec)
        assert paths == EXPECTED_PATHS, (
            f"{label} has {paths} paths, pinned at {EXPECTED_PATHS}. If the API really changed, "
            f"run `{REGENERATE}` and update EXPECTED_PATHS in the same commit."
        )
        assert operations == EXPECTED_OPERATIONS, (
            f"{label} has {operations} operations, pinned at {EXPECTED_OPERATIONS}. If the API "
            f"really changed, run `{REGENERATE}` and update EXPECTED_OPERATIONS in the same commit."
        )
        assert has_known_id, f"{label} is missing operationId {KNOWN_OPERATION_ID!r}"

    def test_live_spec_matches_the_pinned_surface(self, live_spec):
        self._assert_surface(live_spec, "live spec")

    def test_snapshot_matches_the_pinned_surface(self, snapshot_spec):
        """A truncated / empty snapshot must fail here even if drift were somehow clean."""
        self._assert_surface(snapshot_spec, "docs/openapi.json")

    def test_no_route_is_born_hidden_from_the_schema(self):
        """`include_in_schema=False` routes are invisible to the drift comparison.

        Hiding an ALREADY-published route is caught (it vanishes from the export
        and the gate reports it missing from code), but a route added hidden is in
        neither document, so nothing fires. Pinning the hidden set closes that.
        """
        import main

        # getattr for both: Starlette's BaseRoute declares neither attribute —
        # Mount and WebSocketRoute legitimately lack them.
        hidden = {
            str(getattr(route, "path", ""))
            for route in main.app.routes
            if getattr(route, "include_in_schema", True) is False
        } - FRAMEWORK_DOC_ROUTES
        assert hidden == set(HIDDEN_ROUTES), (
            f"the set of routes hidden from the OpenAPI schema changed: {sorted(hidden)}. "
            "A hidden route is undocumented and invisible to the drift gate — publish it, "
            "or add it to HIDDEN_ROUTES with a reason."
        )

    def test_strip_volatile_preserves_the_api_surface(self, live_spec):
        """strip_volatile must not be able to delete the content being compared."""
        stripped = strip_volatile(live_spec)
        assert set(stripped["paths"]) == set(live_spec["paths"])
        assert _operation_ids(stripped) == _operation_ids(live_spec)
        assert len(stripped.get("components", {}).get("schemas", {})) == len(
            live_spec.get("components", {}).get("schemas", {})
        )

    def test_strip_volatile_does_not_mutate_its_input(self, live_spec):
        before = json.dumps(live_spec, sort_keys=True)
        strip_volatile(live_spec)
        assert json.dumps(live_spec, sort_keys=True) == before

    def test_exporter_and_gate_share_one_env_block(self):
        """One env definition, forced — see scripts/openapi_env.py for why.

        ``os.environ.setdefault`` loses to an already-exported variable, which is
        how a test passes locally and fails only in CI.
        """
        source = EXPORTER.read_text()
        assert "from scripts.openapi_env import" in source
        assert "environ.setdefault(" not in source
        assert "environ.setdefault(" not in (ROOT / "scripts" / "openapi_env.py").read_text()


class TestEveryPublishedSuccessBodyIsTyped:
    """No operation may publish ``200`` with a bare ``{}`` schema.

    An empty schema is syntactically valid OpenAPI meaning "any JSON", so nothing
    in the drift gate above notices it: both documents agree, and both are useless.
    What it costs is downstream — orval and every other generator emit
    ``unknown``/``any`` for that response, so the consumer repos (license-authz,
    fr-backend, admin_app, the n8n custom nodes) get no types for the endpoint at
    all. Ten of the 43 operations were in that state when this test was written.

    ⚠️ The cause is almost always a handler returning a raw ``JSONResponse``: the
    route then has no ``response_model``, and adding one is documentation only —
    FastAPI skips ``response_model`` entirely for a returned ``Response`` (see the
    module docstring of ``test_openapi_status_agreement.py``). The fix is to return
    the plain dict/model, which is also what makes the published schema enforced.
    """

    @staticmethod
    def _untyped(spec: dict[str, Any]) -> list[str]:
        return sorted(oid for oid, schema in _json_200_schemas(spec).items() if schema == {})

    def test_the_scan_sees_the_whole_published_surface(self, snapshot_spec):
        """Positive control: without it, a parser that finds nothing passes below.

        Three independent counts, all exact — the total operation count, the number
        of operations the scan actually inspected, and the identity of the ones it
        deliberately did not.
        """
        operations = _operation_count(snapshot_spec)
        inspected = _json_200_schemas(snapshot_spec)
        skipped = _operation_ids(snapshot_spec) - set(inspected)
        assert operations == EXPECTED_OPERATIONS, (
            f"docs/openapi.json has {operations} operations, pinned at {EXPECTED_OPERATIONS} — "
            f"run `{REGENERATE}` and update the pin in the same commit."
        )
        assert len(inspected) == EXPECTED_TYPED_JSON_200_OPERATIONS, (
            f"the scan inspected {len(inspected)} operations, pinned at "
            f"{EXPECTED_TYPED_JSON_200_OPERATIONS}; it is not looking at the whole surface."
        )
        assert skipped == set(OPERATIONS_WITHOUT_A_JSON_200), (
            f"the set of operations publishing no 200 JSON body changed: {sorted(skipped)}. "
            "A 200 body that disappeared is not an operation this test may stop checking."
        )

    def test_snapshot_publishes_no_untyped_200_schema(self, snapshot_spec):
        untyped = self._untyped(snapshot_spec)
        assert untyped == [], (
            f"{len(untyped)} operation(s) publish `200` with an empty `{{}}` schema, so generated "
            f"clients type their response as `unknown`: {untyped}. Give each route a "
            "`response_model=` AND return the plain dict/model rather than a JSONResponse."
        )

    def test_live_spec_publishes_no_untyped_200_schema(self, live_spec):
        """The same assertion against the code, so the fix cannot live only in the snapshot."""
        untyped = self._untyped(live_spec)
        assert untyped == [], f"operations with an empty `200` schema in the live export: {untyped}"

    def test_the_scan_reports_an_untyped_schema(self, snapshot_spec):
        """Discriminating power: a scan that can only return ``[]`` is not a gate."""
        mutated = copy.deepcopy(snapshot_spec)
        victim = mutated["paths"]["/offerings"]["get"]
        assert victim["operationId"] == "list_offerings"
        victim["responses"]["200"]["content"]["application/json"]["schema"] = {}
        assert self._untyped(mutated) == ["list_offerings"]


class TestSchemasAreEnforcedNotJustPublished:
    """The hole the test above cannot see.

    ``TestEveryPublishedSuccessBodyIsTyped`` reads the DOCUMENT, so it goes green
    the moment a ``response_model=`` appears on the decorator. But FastAPI applies
    ``response_model`` only on the branch that serialises a plain return value::

        if isinstance(raw_response, Response):
            response = raw_response            # <- returned VERBATIM
        else:
            content = await serialize_response(...)   # <- response_model applied here

    So re-introducing ``return JSONResponse(...)`` on any of the ten routes leaves
    the published schema intact and typed while the runtime check silently
    disappears — the document keeps promising a shape nothing verifies. That is the
    state all ten were in before this branch, and it is invisible to every
    document-level gate in this file.
    """

    def test_the_typed_handlers_return_a_plain_value(self):
        offenders = sorted(
            oid
            for oid in OPERATIONS_WHOSE_SCHEMA_IS_ENFORCED
            if _handler_returns_a_raw_response(oid)
        )
        assert offenders == [], (
            f"{offenders} return a raw Response, so FastAPI skips their `response_model` — "
            "the schema is published but not enforced. Return the plain dict/model, or move "
            "the operation to OPERATIONS_WITH_A_DELIBERATE_RAW_RESPONSE with the reason."
        )

    def test_the_detector_fires_on_a_handler_that_does_return_one(self):
        """Positive control, measured on real routes rather than a straw man.

        Without it, a detector that can only return False passes the test above on
        every possible input — including the pre-fix tree it was written for.
        """
        found = sorted(
            oid
            for oid in OPERATIONS_WITH_A_DELIBERATE_RAW_RESPONSE
            if _handler_returns_a_raw_response(oid)
        )
        assert found == sorted(OPERATIONS_WITH_A_DELIBERATE_RAW_RESPONSE), (
            "the dual-status routes are supposed to return a raw Response; the detector "
            f"only saw {found}, so it is not detecting anything."
        )

    def test_every_enforced_operation_really_declares_a_response_model(self, snapshot_spec):
        """A returned plain dict with no ``response_model`` is untyped, not enforced.

        Both halves are needed, and this one pins the half the class name claims.
        """
        published = _json_200_schemas(snapshot_spec)
        missing = sorted(OPERATIONS_WHOSE_SCHEMA_IS_ENFORCED - set(published))
        assert missing == [], f"{missing} publish no 200 JSON body at all"
        untyped = sorted(oid for oid in OPERATIONS_WHOSE_SCHEMA_IS_ENFORCED if published[oid] == {})
        assert untyped == [], f"{untyped} are serialised through nothing"


class TestGateDiscriminatingPower:
    """Prove the comparison reports each class of change it exists to catch.

    Every case mutates an in-memory deep copy; nothing on disk is touched.
    """

    def test_identical_specs_report_clean(self, live_spec):
        """Negative control — without this, a report that is never empty is 'passing'."""
        assert drift_report(live_spec, copy.deepcopy(live_spec)) == []

    def test_foreign_tenant_id_is_actually_foreign(self):
        """The probe value must be one no code path can produce.

        If FOREIGN_TENANT_ID were a value the service itself emits, the tests
        below would agree by coincidence and retire themselves silently. That is
        not hypothetical — a mutation run caught exactly that. Asserted, because a
        comment cannot fail.
        """
        assert FOREIGN_TENANT_ID not in NON_FOREIGN_TENANT_IDS

    def test_no_concrete_tenant_id_is_published_in_the_tenant_header(self, live_spec):
        """The X-Tenant-Id parameter must document no default and no example.

        A generated client materialises a documented default into real requests,
        and in this fleet a bare X-Tenant-Id is the gateway's production contract —
        so publishing any concrete tenant id (``n8n-dev`` is a REAL tenant) turns
        "I used the documented default" into a cross-tenant request.
        """
        header = live_spec["components"]["parameters"]["TenantIdHeader"]
        assert "example" not in header, f"X-Tenant-Id publishes example={header.get('example')!r}"
        assert "default" not in header.get("schema", {}), (
            f"X-Tenant-Id publishes schema.default={header['schema'].get('default')!r}"
        )
        for forbidden in {*NON_FOREIGN_TENANT_IDS, FOREIGN_TENANT_ID}:
            assert forbidden not in json.dumps(header), (
                f"a concrete tenant id ({forbidden!r}) reached the X-Tenant-Id parameter"
            )

    def test_no_environment_value_can_reach_the_published_document(
        self, live_spec, foreign_env_spec
    ):
        """Exporting under a hostile environment must produce the identical document.

        ``SERVICE_NAME``/``VERSION`` are pinned by ``scripts/openapi_env.py``;
        ``DEFAULT_TENANT_ID`` no longer reaches the document at all now that the
        parameter carries no default or example. Reintroducing either route fails
        here rather than quietly republishing a real tenant id.
        """
        assert live_spec == foreign_env_spec
        assert FOREIGN_TENANT_ID not in json.dumps(live_spec)
        assert live_spec["info"]["title"] == "licensing-service"

    def test_two_exports_under_different_env_are_not_drift(self):
        """Two honest exports of the same code under different env must not read as drift."""
        theirs = _export_without_the_pin({"DEFAULT_TENANT_ID": FOREIGN_TENANT_ID, "VERSION": "7.7"})
        ours = _export_without_the_pin({"DEFAULT_TENANT_ID": "n8n-dev", "VERSION": "1.2.3"})
        # Positive control: the env really did move the document (via VERSION).
        # Without it, the next assertion could pass on two identical inputs.
        assert theirs != ours
        assert drift_report(theirs, ours) == []
        # DEFAULT_TENANT_ID, by contrast, must move nothing at all any more.
        assert FOREIGN_TENANT_ID not in json.dumps(theirs)

    def test_env_derived_fields_alone_are_not_drift(self, live_spec):
        """The two fields main.py bakes from env must not make the gate permanently red."""
        stale = copy.deepcopy(live_spec)
        stale["info"]["version"] = "9.9.9-someone-elses-build"
        stale["info"]["title"] = "licensing-service-local"
        assert drift_report(stale, live_spec) == []

    def test_tenant_header_schema_change_is_reported(self, live_spec):
        """The gate must SEE this parameter — it was blind to it until 2026-08-10.

        `strip_volatile` used to drop `TenantIdHeader.schema.default`/`example`,
        so the published contract could document any tenant id with nothing to
        catch it. With the fields gone from the document, the strip is gone too
        and the whole parameter is compared.
        """
        stale = copy.deepcopy(live_spec)
        stale["components"]["parameters"]["TenantIdHeader"]["schema"]["default"] = "n8n-dev"
        report = drift_report(stale, live_spec)
        assert report, "a resurrected X-Tenant-Id default must be reported"
        text = "\n".join(report)
        assert "TenantIdHeader" in text
        assert "n8n-dev" in text

    def test_path_missing_from_snapshot_is_reported_as_added_by_code(self, live_spec):
        stale = copy.deepcopy(live_spec)
        del stale["paths"]["/licenses/admin/metering/reconcile"]
        report = drift_report(stale, live_spec)
        assert report, "deleting a path from the snapshot must be reported"
        text = "\n".join(report)
        assert "/licenses/admin/metering/reconcile" in text
        assert "added by code" in text

    def test_ghost_path_in_snapshot_is_reported_as_missing_from_code(self, live_spec):
        stale = copy.deepcopy(live_spec)
        stale["paths"]["/ghost"] = {
            "get": {"operationId": "ghost", "responses": {"200": {"description": "ok"}}}
        }
        report = drift_report(stale, live_spec)
        assert report, "a snapshot-only path must be reported"
        text = "\n".join(report)
        assert "/ghost" in text
        assert "missing from code" in text

    def test_changed_operation_id_is_reported(self, live_spec):
        """The mutation a path-set-only gate waves through."""
        stale = copy.deepcopy(live_spec)
        stale["paths"]["/license/revoke"]["post"]["operationId"] = "revoke_token_OLD"
        assert set(stale["paths"]) == set(live_spec["paths"]), "path sets must be identical here"
        report = drift_report(stale, live_spec)
        assert report, "a renamed operationId must be reported"
        text = "\n".join(report)
        assert "operationId" in text
        assert "revoke_token_OLD" in text

    def test_changed_schema_property_type_is_reported(self, live_spec):
        """The other mutation a path-set-only gate waves through."""
        stale = copy.deepcopy(live_spec)
        pointer = self._first_typed_property(stale)
        assert pointer is not None, "no schema property with a plain `type` found to mutate"
        schema_name, property_name = pointer
        prop = stale["components"]["schemas"][schema_name]["properties"][property_name]
        original_type = prop["type"]
        prop["type"] = "integer" if original_type != "integer" else "string"
        assert set(stale["paths"]) == set(live_spec["paths"]), "path sets must be identical here"
        report = drift_report(stale, live_spec)
        assert report, f"changing {schema_name}.{property_name}.type must be reported"
        text = "\n".join(report)
        assert schema_name in text
        assert property_name in text

    @staticmethod
    def _first_typed_property(spec: dict[str, Any]) -> tuple[str, str] | None:
        for schema_name in sorted(spec.get("components", {}).get("schemas", {})):
            schema = spec["components"]["schemas"][schema_name]
            for property_name in sorted(schema.get("properties", {}) or {}):
                prop = schema["properties"][property_name]
                if isinstance(prop, dict) and isinstance(prop.get("type"), str):
                    return schema_name, property_name
        return None
