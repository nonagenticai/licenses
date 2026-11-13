"""Licenses-side half of the licences -> billing response-contract handshake.

billing-service vendors the ``GET /internal/licenses`` 200 response schema (and
every schema it ``$ref``s) into its ``tests/support/licences_contract.json`` and
reads production fields out of it. Its freshness check can only diff against
this repo with a checkout present, which a billing CI runner never has.

The only CI that always sees this repo's spec is this repo's. So the pin lives
here: ``docs/consumer_contracts/billing-service.json`` records a sha256 of that
response schema and of each referenced schema, and this test recomputes them
from ``docs/openapi.json`` (tied to the live app by ``test_openapi_drift.py``)
on every run, with no skip path. billing records the same digests in its
copy's ``_source`` and asserts its copy still hashes to them on every run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_SPEC_PATH = _ROOT / "docs" / "openapi.json"
_PIN_PATH = _ROOT / "docs" / "consumer_contracts" / "billing-service.json"
_PATH, _METHOD = "/internal/licenses", "get"


def schema_digest(schema: Any) -> str:
    """Canonical digest. MUST match billing's ``licences_contract.schema_digest``."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mirrored_digests(spec: dict[str, Any]) -> dict[str, Any]:
    """What billing mirrors, recomputed: same closure as its refresher."""
    components = spec["components"]["schemas"]
    response = spec["paths"][_PATH][_METHOD]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    wanted: set[str] = set()
    frontier = [json.dumps(response)]
    while frontier:
        blob = frontier.pop()
        for name in re.findall(r"#/components/schemas/(\w+)", blob):
            if name not in wanted:
                wanted.add(name)
                frontier.append(json.dumps(components[name]))
    return {
        "response_sha256": schema_digest(response),
        "schema_sha256": [
            {"schema": name, "sha256": schema_digest(components[name])} for name in sorted(wanted)
        ],
    }


def _pin() -> dict[str, Any]:
    pin = json.loads(_PIN_PATH.read_text())
    return {"response_sha256": pin["response_sha256"], "schema_sha256": pin["schema_sha256"]}


def test_the_licences_response_billing_mirrors_is_unchanged_or_its_pin_was_moved() -> None:
    pin = _pin()
    # Precondition: an empty pin would compare equal to an empty closure.
    assert {e["schema"] for e in pin["schema_sha256"]} == {
        "AppInfo",
        "InternalLicenseResponse",
        "PlanInfo",
    }, pin
    live = mirrored_digests(json.loads(_SPEC_PATH.read_text()))
    assert live == pin, (
        f"GET {_PATH} as published in docs/openapi.json no longer matches what "
        f"billing-service mirrors.\n  pinned: {pin}\n  now:    {live}\n"
        + json.loads(_PIN_PATH.read_text())["_meta"]["on_failure"]
    )


def test_the_pin_check_fails_when_a_mirrored_schema_changes() -> None:
    """Non-vacuity: one retyped field in one referenced schema moves exactly its digest."""
    spec = json.loads(_SPEC_PATH.read_text())
    mutated = copy.deepcopy(spec)
    mutated["components"]["schemas"]["PlanInfo"]["properties"]["injected"] = {"type": "string"}
    pinned = {e["schema"]: e["sha256"] for e in _pin()["schema_sha256"]}
    now = {e["schema"]: e["sha256"] for e in mirrored_digests(mutated)["schema_sha256"]}
    assert {n for n in pinned if pinned[n] != now[n]} == {"PlanInfo"}


def test_the_pin_check_fails_when_the_response_gains_a_new_referenced_schema() -> None:
    """A newly ``$ref``'d schema changes the covered SET, not just a digest."""
    spec = json.loads(_SPEC_PATH.read_text())
    mutated = copy.deepcopy(spec)
    mutated["components"]["schemas"]["Injected"] = {"type": "object"}
    plan = mutated["components"]["schemas"]["PlanInfo"]
    plan["properties"]["injected"] = {"$ref": "#/components/schemas/Injected"}
    assert mirrored_digests(mutated) != _pin()
    assert "Injected" in {e["schema"] for e in mirrored_digests(mutated)["schema_sha256"]}
