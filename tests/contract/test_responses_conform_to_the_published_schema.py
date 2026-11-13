"""Every response this service actually returns must match the schema it publishes.

`tests/contract/` has existed since the initial commit holding nothing but
`schemathesis_hooks.py` — a hooks module imported by no test. `schemathesis` is
declared in the `contract` extra, and the CI archetype runs `uv sync --all-extras`,
so it has been INSTALLED on every CI run for a test that was never written. The
directory read as "contract testing exists" while none did.

WHY THIS IS A VALIDATOR AND NOT A FUZZER. Schemathesis is best known for generating
requests. That is the wrong tool here: this service's operations write to a database,
and hypothesis-driven POST/DELETE traffic against them buys flakiness and a slow
suite in exchange for coverage the unit tests already have. What is NOT covered
anywhere else is the other direction — whether the bodies these routes actually
return match the schemas `docs/openapi.json` promises to a generated client. So the
requests are fixed and hand-written; only the RESPONSE validation comes from
schemathesis, which is the part that would otherwise mean reimplementing a JSON
Schema validator.

This is worth having precisely because the published schemas are new: ten operations
carried `"schema": {}` until they were typed, and `{}` conforms to everything. A typed
schema that nothing validates against is a claim, not a contract.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

# CI installs the `contract` extra (`uv sync --all-extras` in the archetype), so a
# missing import there is a real failure and must not be skipped past. Locally a
# developer may have synced without extras; skipping for them is a convenience, not a
# hole, because CI is where the gate has to hold. Making this unconditional would go
# red on every checkout that did not opt into the extra — a guard nobody keeps.
if os.getenv("CI"):
    import schemathesis
else:
    schemathesis = pytest.importorskip("schemathesis")

from schemathesis.openapi.checks import JsonSchemaError  # noqa: E402


def _schema(client: TestClient) -> Any:
    return schemathesis.openapi.from_dict(client.app.openapi())


def _operation(client: TestClient, method: str, path: str) -> Any:
    """The schema's operation for (method, path), by identity rather than by index."""
    for result in _schema(client).get_all_operations():
        operation = result.ok()
        if operation.method.upper() == method.upper() and operation.path == path:
            return operation
    raise AssertionError(f"{method} {path} is not in the published schema")


def _declared_success_schema(operation: Any, status: str = "200") -> dict[str, Any]:
    """The schema the operation publishes for ``status``, or ``{}`` if it publishes none."""
    responses = operation.definition.raw.get("responses", {})
    content = responses.get(status, {}).get("content", {})
    return content.get("application/json", {}).get("schema", {})


def _validate(
    client: TestClient, method: str, path: str, response: Any, status: str = "200"
) -> None:
    """Validate a response AND refuse to do so vacuously.

    `validate_response` alone is not a gate. An operation that publishes ``"schema": {}``
    conforms to EVERY body, so a route losing its `response_model` leaves these tests
    green — measured, not theorised: removing `response_model=TenantByoModeResponse`
    from `get_tenant_byo_mode` left all 14 tests passing. The conformance check cannot
    see the difference between "the body matches" and "nothing was required of it".

    So the typed-ness of the schema is asserted FIRST, and only then is the body
    validated against it. With this line, that same mutant goes red here.
    """
    declared = _declared_success_schema(_operation(client, method, path), status)
    assert declared, (
        f"{method} {path} publishes an empty {status} schema — `{{}}` conforms to every "
        "body, so validating against it proves nothing"
    )
    _operation(client, method, path).validate_response(response)


class TestTheCollectionReadsMatchTheirSchema:
    """The GET surface, driven with REAL rows in the tables.

    Validating against empty collections is the easy half and proves little: `[]`
    satisfies almost any array schema, including a wrong one. Every case here runs
    with `full_seed_data`, so the item schema is exercised, not just the envelope.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/apps",
            "/plans",
            "/regions",
            "/tenants",
            "/offerings",
            "/licenses",
            "/licenses/admin/all",
            "/internal/licenses",
        ],
    )
    def test_the_body_matches_the_declared_schema(
        self, auth_client_admin: TestClient, full_seed_data: dict[str, Any], path: str
    ) -> None:
        response = auth_client_admin.get(path)
        assert response.status_code == 200, response.text
        _validate(auth_client_admin, "GET", path, response)

    def test_the_seeded_reads_are_not_empty(
        self, auth_client_admin: TestClient, full_seed_data: dict[str, Any]
    ) -> None:
        """Anti-vacuity for the class above.

        If seeding silently stopped working, every body would be `[]`, every
        validation would pass, and the suite would report a contract it never checked.
        """
        for path in ("/apps", "/plans", "/licenses"):
            payload = auth_client_admin.get(path).json()
            items = payload if isinstance(payload, list) else payload.get("licenses", payload)
            assert items, f"{path} returned an empty body — the item schema went unchecked"


class TestTheNewlyTypedOperationsAreChecked:
    """The ten operations that published `"schema": {}` until they were typed.

    `{}` conforms to everything, so before they were typed these validations could not
    have failed. They are listed explicitly rather than discovered, so that an
    operation losing its schema is a FAILURE here and not a silently smaller loop.
    """

    def test_get_tenant_byo_mode(self, auth_client_admin: TestClient, seed_license: Any) -> None:
        tenant_id = str(seed_license.tenant_id)
        response = auth_client_admin.get(f"/tenants/{tenant_id}/byo-mode")
        assert response.status_code == 200, response.text
        _validate(auth_client_admin, "GET", "/tenants/{name_or_id}/byo-mode", response)

    def test_list_offerings(
        self, auth_client_admin: TestClient, full_seed_data: dict[str, Any]
    ) -> None:
        response = auth_client_admin.get("/offerings")
        assert response.status_code == 200, response.text
        _validate(auth_client_admin, "GET", "/offerings", response)


class TestTheValidatorDiscriminates:
    """The control. A validator that cannot fail is a slower way of asserting True.

    Every test above passes today; that is only evidence if a WRONG body is refused.

    The obvious way to write this — hand-build an `httpx.Response` carrying a bad body —
    DOES NOT WORK, and fails in the direction that looks like success. A synthetic
    response has no `.elapsed` (only a real transport sets it), so `validate_response`
    raises `RuntimeError: '.elapsed' may only be accessed after the response has been
    read or closed` BEFORE it ever looks at the body. Caught with `pytest.raises(Exception)`
    that reads as a passing control while proving nothing about the schema at all.

    So the mismatch is built from two REAL responses instead: `/apps` returns a JSON
    array and `/health` a JSON object, so validating each against the other's operation
    must fail, and the specific `JsonSchemaError` is required rather than any exception.
    """

    def test_an_array_body_fails_an_object_schema(
        self, auth_client_admin: TestClient, full_seed_data: dict[str, Any]
    ) -> None:
        with pytest.raises(JsonSchemaError):
            _operation(auth_client_admin, "GET", "/health").validate_response(
                auth_client_admin.get("/apps")
            )

    def test_an_object_body_fails_an_array_schema(
        self, auth_client_admin: TestClient, full_seed_data: dict[str, Any]
    ) -> None:
        with pytest.raises(JsonSchemaError):
            _operation(auth_client_admin, "GET", "/apps").validate_response(
                auth_client_admin.get("/health")
            )

    def test_each_body_still_passes_its_own_schema(
        self, auth_client_admin: TestClient, full_seed_data: dict[str, Any]
    ) -> None:
        """Pairs with the two above: the refusals are about the BODY, not the setup.

        Without this, both rejections would also be explained by the validator being
        broken for every input.
        """
        _operation(auth_client_admin, "GET", "/apps").validate_response(
            auth_client_admin.get("/apps")
        )
        _operation(auth_client_admin, "GET", "/health").validate_response(
            auth_client_admin.get("/health")
        )
