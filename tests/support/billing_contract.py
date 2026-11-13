"""Contract guard for the request bodies licenses POSTs to billing-service.

Why this exists
---------------
``_provision_budget`` builds two JSON bodies by hand and POSTs them to
billing-service (``/api/budget`` -> ``TokenBudgetCreate``, ``/api/quota/config``
-> ``QuotaConfigRequest``). Nothing in this repo imports billing's models, so a
field billing REQUIRES but licenses omits is a silent, deterministic 422 that no
unit test with a signature-agnostic ``AsyncMock`` can ever see. That is exactly
how the missing ``tenant_id`` on the quota body shipped and made request-quota
registration fail 100% of the time.

Why the schemas are VENDORED (and not read from ../../billing_service)
----------------------------------------------------------------------
The authoritative file is ``billing_service/docs/openapi.json`` — a different
repo. Reading it by relative path would make this guard *conditional*: on a CI
runner (which checks out only ``licenses``) the file is absent, and the only
options are "skip" (a guard that skips itself into green is worse than no guard)
or "fail on every CI run" (unusable). So the two schemas are vendored into
``tests/support/billing_contract.json`` with their source commit recorded, and
the guard ALWAYS runs, everywhere, with no skip path.

Freshness is handled separately and explicitly by
``test_billing_contract_copy_matches_source``: when the sibling repo IS present
it diffs the vendored copy against the live file and fails loudly on drift; only
that *freshness* check may skip, and it says why. Refresh with
``python3 tests/support/refresh_billing_contract.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

_CONTRACT_PATH = Path(__file__).with_name("billing_contract.json")

_SPEC_RELATIVE = Path("docs") / "openapi.json"

#: Sibling directory names searched for. ``billing_service`` is the ~/Movies
#: convention; ``billing-service`` is the GitHub slug, i.e. a plain ``git clone``.
_SOURCE_DIR_NAMES = ("billing_service", "billing-service")

#: Explicit checkout root. When SET it is authoritative: a path without
#: ``docs/openapi.json`` is an ERROR, never a fallback to "no source, skip".
SOURCE_REPO_ENV = "BILLING_SERVICE_REPO"

#: billing-service's pin of the digests of exactly the schemas vendored here.
#: Its own CI fails when one of those schemas changes, which is the half of the
#: freshness handshake that runs on every billing PR with no sibling needed.
_PIN_RELATIVE = Path("docs") / "consumer_contracts" / "licenses.json"


class UncheckableSchemaError(NotImplementedError):
    """The vendored schema uses a construct this guard cannot validate.

    RAISED, never returned as a violation string. An unimplemented keyword is a
    defect in the GUARD, not in the body, and conflating the two is how a guard
    ends up reporting green while validating nothing:

    * a caller with an exact-list carve-out (``assert violations(...) == [...]``,
      as ``test_zero_budget_body_only_breaches_billings_gt0_floor`` has) would
      absorb it as just another expected string;
    * "no violations" would keep meaning "checked and clean" for a field that
      was never checked at all.

    So the deliberate posture is fail-loud: if billing adds ``maxLength``,
    ``pattern``, an ``allOf``, or a ``format`` this module does not implement,
    EVERY test using the guard errors with "I do not know how to check this"
    until the keyword is implemented below. The alternative — skip the unknown
    keyword — is silent scope loss, which is precisely the class of bug this
    module was written to catch.
    """


# Keywords that carry no validation semantics: skipping them checks nothing away.
_ANNOTATION_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "example",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$comment",
        "discriminator",
    }
)

# Keywords this guard actually implements. Anything else -> UncheckableSchemaError.
_CHECKED_KEYWORDS = frozenset(
    {
        "$ref",
        "anyOf",
        "oneOf",
        "type",
        "enum",
        "const",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "items",
        "properties",
        "required",
    }
)

# ``format`` values this guard can actually verify. Unknown formats are loud:
# billing's ``starts_at``/``expires_at`` are ``date-time`` and a non-parsable
# string there is a real 422, so silently ignoring formats is not acceptable.
_CHECKED_FORMATS = frozenset({"date-time", "date", "uuid"})


def load_contract() -> dict[str, Any]:
    """The vendored billing schemas. Never optional — a missing file is an error."""
    return json.loads(_CONTRACT_PATH.read_text())


def schema(name: str) -> dict[str, Any]:
    """Return one vendored schema by name (KeyError if it was never vendored)."""
    schemas = load_contract()["schemas"]
    if name not in schemas:
        raise KeyError(
            f"{name} is not vendored in {_CONTRACT_PATH.name}; "
            "add it via tests/support/refresh_billing_contract.py"
        )
    return schemas[name]


def source_openapi_path() -> Path | None:
    """The live billing openapi.json, if a billing-service checkout is available.

    ``$BILLING_SERVICE_REPO`` wins when set (and raises if it does not hold the
    spec). Otherwise walks up from this file rather than counting
    ``parents[n]``: a plain checkout puts the sibling two levels up, a
    ``.worktrees/<slug>`` checkout four, and hard-coding either makes the
    freshness check silently unable to find a source that IS there.
    """
    explicit = os.environ.get(SOURCE_REPO_ENV)
    if explicit:
        candidate = Path(explicit).resolve() / _SPEC_RELATIVE
        if not candidate.is_file():
            raise FileNotFoundError(
                f"${SOURCE_REPO_ENV}={explicit} is set but {candidate} does not exist"
            )
        return candidate
    for ancestor in Path(__file__).resolve().parents:
        for name in _SOURCE_DIR_NAMES:
            candidate = ancestor / name / _SPEC_RELATIVE
            if candidate.is_file():
                return candidate
    return None


def schema_digest(spec: dict[str, Any]) -> str:
    """Canonical per-schema digest.

    MUST stay byte-identical to ``schema_digest`` in billing-service's
    ``tests/test_consumer_contract_pins.py``: the two repos compare these values
    across the repo boundary, so a different canonicalisation would read as
    drift on every schema.
    """
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def schema_digests(contract: dict[str, Any] | None = None) -> dict[str, str]:
    """``{name: sha256}`` over the vendored schemas, recomputed (not read)."""
    schemas = (contract or load_contract())["schemas"]
    return {name: schema_digest(spec) for name, spec in sorted(schemas.items())}


def recorded_digests(entries: list[dict[str, str]]) -> dict[str, str]:
    """``{name: sha256}`` from a ``schema_sha256`` list (vendored ``_meta`` or billing's pin)."""
    return {entry["schema"]: entry["sha256"] for entry in entries}


def source_pin_path() -> Path | None:
    """billing-service's committed pin of this copy's digests, if a checkout exists."""
    repo = source_repo_path()
    return repo / _PIN_RELATIVE if repo is not None else None


def source_repo_path() -> Path | None:
    """The sibling ``billing_service`` checkout root, if one exists.

    Same ancestor walk as :func:`source_openapi_path`, and for the same reason:
    a plain checkout puts the sibling two levels up, a ``.worktrees/<slug>``
    checkout four. Used by ``refresh_billing_contract.py`` to AST-read billing's
    ROUTE SOURCE — the auth dependency on a route and the role set that
    dependency admits are not in ``openapi.json``, so the spec cannot answer
    "who may write this", which is exactly the fact this repo's docstrings kept
    getting wrong.

    Derived FROM :func:`source_openapi_path` rather than repeating its walk, so
    the two can never point at different checkouts — the failure the refresher's
    ``find_source`` docstring already records.
    """
    source = source_openapi_path()
    return source.parents[1] if source is not None else None


def write_gates(contract: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The vendored ``{route: {dependency, roles}}`` map for billing's money POSTs.

    Vendored for the same reason the schemas are: it must be readable on a CI
    runner that checked out only ``licenses``, with no skip path. Refreshed by
    ``tests/support/refresh_billing_contract.py``.
    """
    gates = (contract or load_contract()).get("write_gates")
    if not gates:
        raise KeyError(
            "write_gates is missing/empty in billing_contract.json — "
            "run python3 tests/support/refresh_billing_contract.py"
        )
    return gates


def _ref_name(prop: dict[str, Any]) -> str:
    return str(prop["$ref"]).rsplit("/", 1)[-1]


def _resolve(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow a ``$ref`` to its vendored target, or fail loudly.

    An UNRESOLVABLE ``$ref`` used to be the guard's widest blind spot: the old
    code returned ``set()`` / ``[]`` for it, and since the type check is
    ``if accepted and actual not in accepted`` an empty ``accepted``
    short-circuits — so a field pointing at a schema nobody vendored accepted
    literally any value, silently. Proven synthetically: a ``$ref`` to an
    unvendored name with body value ``{"totally": "wrong shape"}`` produced no
    complaint at all. It is now an error, not an empty result.
    """
    name = _ref_name(prop)
    target = defs.get(name)
    if target is None:
        raise UncheckableSchemaError(
            f"$ref -> {name!r} cannot be resolved: that schema is not vendored in "
            f"{_CONTRACT_PATH.name}. Add it to VENDORED in "
            "tests/support/refresh_billing_contract.py and re-run that script — "
            "an unresolvable $ref makes the field it guards accept ANY value."
        )
    return target


def _sub_schemas(prop: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``anyOf``/``oneOf`` alternatives of a schema node, if any."""
    return [*prop.get("anyOf", []), *prop.get("oneOf", [])]


def _types_of(prop: dict[str, Any], defs: dict[str, Any]) -> set[str]:
    """Collect the JSON-Schema type names a property accepts (anyOf/$ref aware)."""
    if "$ref" in prop:
        return _types_of(_resolve(prop, defs), defs)
    alternatives = _sub_schemas(prop)
    if alternatives:
        types: set[str] = set()
        for branch in alternatives:
            types |= _types_of(branch, defs)
        return types
    declared = prop.get("type")
    return {declared} if isinstance(declared, str) else set()


def _branches(prop: dict[str, Any], defs: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a property into the concrete schema dicts that carry constraints.

    ``$ref`` and ``anyOf`` must be followed here too: billing declares
    ``period`` as a bare ``$ref`` to the ``BudgetPeriod`` enum, so a checker that
    only looks at the top-level property dict silently validates NOTHING for it.
    """
    if "$ref" in prop:
        return _branches(_resolve(prop, defs), defs)
    alternatives = _sub_schemas(prop)
    if alternatives:
        out: list[dict[str, Any]] = []
        for branch in alternatives:
            out.extend(_branches(branch, defs))
        return out
    return [prop]


def _iter_refs(node: Any) -> Iterator[str]:
    """Every ``$ref`` target name appearing anywhere under ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref.rsplit("/", 1)[-1]
        for value in node.values():
            yield from _iter_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_refs(value)


def unvendored_refs(contract: dict[str, Any] | None = None) -> list[str]:
    """Schema names ``$ref``'d from a vendored schema but not themselves vendored.

    ``refresh_billing_contract.py`` pins the vendored set BY HAND
    (``VENDORED = (...)``). A hand-listed scope is only ever correct by
    coincidence: the moment billing turns a field into a nested model or an
    enum, the hand-list undercounts and the new ``$ref`` dangles. Since a
    dangling ``$ref`` makes its field accept anything, "closed under ``$ref``"
    is an invariant of this file, not a nicety — asserted by
    ``test_vendored_schema_set_is_closed_under_ref``.
    """
    defs = (contract or load_contract())["schemas"]
    return sorted(set(_iter_refs(defs)) - set(defs))


def assert_ref_closure(contract: dict[str, Any] | None = None) -> None:
    """Hard assertion: the vendored schema set is closed under ``$ref``."""
    dangling = unvendored_refs(contract)
    assert not dangling, (
        f"vendored schemas $ref these NON-vendored schemas: {dangling}. "
        "Each one silently accepts ANY value for the field that points at it. "
        "Add them to VENDORED in tests/support/refresh_billing_contract.py and "
        "re-run it."
    )


def _assert_checkable(
    schema_name: str,
    spec: dict[str, Any],
    defs: dict[str, Any],
    path: str = "",
    seen: frozenset[str] = frozenset(),
) -> None:
    """Fail loudly if any part of ``spec`` uses a construct we cannot validate.

    Runs over the SCHEMA, independently of any body, so an unimplemented
    keyword cannot hide behind a value that happens not to exercise it.
    """
    where = f"{schema_name}.{path}" if path else schema_name

    unknown = set(spec) - _CHECKED_KEYWORDS - _ANNOTATION_KEYWORDS
    if unknown:
        raise UncheckableSchemaError(
            f"{where}: billing declares keyword(s) {sorted(unknown)} that this guard "
            "does not implement, so that constraint would be silently unchecked. "
            "Implement it in tests/support/billing_contract.py (see _CHECKED_KEYWORDS)."
        )

    fmt = spec.get("format")
    if fmt is not None and fmt not in _CHECKED_FORMATS:
        raise UncheckableSchemaError(
            f"{where}: format={fmt!r} is not implemented by this guard "
            f"(known: {sorted(_CHECKED_FORMATS)}); implement it or a bad value passes."
        )

    if "$ref" in spec:
        name = _ref_name(spec)
        target = _resolve(spec, defs)
        if name not in seen:
            _assert_checkable(schema_name, target, defs, path, seen | {name})

    for index, branch in enumerate(_sub_schemas(spec)):
        _assert_checkable(schema_name, branch, defs, f"{path}[{index}]" if path else f"[{index}]")

    for prop_name, prop in spec.get("properties", {}).items():
        _assert_checkable(
            schema_name, prop, defs, f"{path}.{prop_name}" if path else prop_name, seen
        )

    items = spec.get("items")
    if isinstance(items, dict):
        _assert_checkable(schema_name, items, defs, f"{path}[]", seen)


def _json_type(value: Any) -> str:
    # bool before int: ``isinstance(True, int)`` is True in Python and would
    # otherwise let a boolean satisfy an ``integer`` field.
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _format_ok(value: str, fmt: str) -> bool:
    """Whether ``value`` parses as ``fmt``. Only ``_CHECKED_FORMATS`` reach here."""
    try:
        if fmt == "date-time":
            datetime.fromisoformat(value)
        elif fmt == "date":
            date.fromisoformat(value)
        elif fmt == "uuid":
            UUID(value)
    except ValueError:
        return False
    return True


def _check_branch(
    schema_name: str,
    path: str,
    value: Any,
    actual: str,
    branch: dict[str, Any],
    defs: dict[str, Any],
    found: list[str],
) -> None:
    """Apply one resolved branch's constraints to ``value``."""
    if "const" in branch and value != branch["const"]:
        found.append(
            f"{schema_name}: field {path!r}={value!r} is not the required const {branch['const']!r}"
        )
    if "enum" in branch and value not in branch["enum"]:
        found.append(f"{schema_name}: field {path!r}={value!r} is not one of {branch['enum']}")

    if actual in ("integer", "number"):
        if "exclusiveMinimum" in branch and not value > branch["exclusiveMinimum"]:
            found.append(
                f"{schema_name}: field {path!r}={value} violates "
                f"exclusiveMinimum={branch['exclusiveMinimum']}"
            )
        if "minimum" in branch and value < branch["minimum"]:
            found.append(
                f"{schema_name}: field {path!r}={value} violates minimum={branch['minimum']}"
            )
        if "exclusiveMaximum" in branch and not value < branch["exclusiveMaximum"]:
            found.append(
                f"{schema_name}: field {path!r}={value} violates "
                f"exclusiveMaximum={branch['exclusiveMaximum']}"
            )
        if "maximum" in branch and value > branch["maximum"]:
            found.append(
                f"{schema_name}: field {path!r}={value} violates maximum={branch['maximum']}"
            )
        if "multipleOf" in branch and value % branch["multipleOf"] != 0:
            found.append(
                f"{schema_name}: field {path!r}={value} violates multipleOf={branch['multipleOf']}"
            )

    if actual == "string":
        if len(value) < branch.get("minLength", 0):
            found.append(
                f"{schema_name}: field {path!r} is shorter than minLength={branch['minLength']}"
            )
        if "maxLength" in branch and len(value) > branch["maxLength"]:
            found.append(
                f"{schema_name}: field {path!r} is longer than maxLength={branch['maxLength']}"
            )
        if "pattern" in branch and re.search(branch["pattern"], value) is None:
            found.append(
                f"{schema_name}: field {path!r}={value!r} does not match "
                f"pattern {branch['pattern']!r}"
            )
        if "format" in branch and not _format_ok(value, branch["format"]):
            found.append(
                f"{schema_name}: field {path!r}={value!r} is not a valid {branch['format']}"
            )

    if actual == "array":
        if len(value) < branch.get("minItems", 0):
            found.append(
                f"{schema_name}: field {path!r} has {len(value)} items, "
                f"violates minItems={branch['minItems']}"
            )
        if "maxItems" in branch and len(value) > branch["maxItems"]:
            found.append(
                f"{schema_name}: field {path!r} has {len(value)} items, "
                f"violates maxItems={branch['maxItems']}"
            )
        items = branch.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                _check_value(schema_name, f"{path}[{index}]", element, items, defs, found)

    # A nested object carries its OWN required set and properties. Without this
    # recursion a nested model missing a required field passed unnoticed: the
    # outer type check saw "object", matched, and stopped there.
    if actual == "object" and ("properties" in branch or "required" in branch):
        _check_object(schema_name, value, branch, defs, found, prefix=f"{path}.")


def _check_value(
    schema_name: str,
    path: str,
    value: Any,
    prop: dict[str, Any],
    defs: dict[str, Any],
    found: list[str],
) -> None:
    """Type-check ``value`` against ``prop`` then apply the matching branches."""
    accepted = _types_of(prop, defs)
    actual = _json_type(value)
    if accepted and actual not in accepted:
        found.append(
            f"{schema_name}: field {path!r} has type {actual} but billing declares {sorted(accepted)}"
        )
        return
    # Constraints live on the resolved branch(es) whose declared type the
    # value actually matches — never on the outer ``$ref``/``anyOf`` wrapper.
    for branch in _branches(prop, defs):
        if branch.get("type") not in (None, actual):
            continue
        _check_branch(schema_name, path, value, actual, branch, defs, found)


def _check_object(
    schema_name: str,
    body: dict[str, Any],
    spec: dict[str, Any],
    defs: dict[str, Any],
    found: list[str],
    prefix: str = "",
) -> None:
    """Required-field, undeclared-property and per-field checks for one object."""
    props: dict[str, Any] = spec.get("properties", {})
    required: list[str] = spec.get("required", [])

    for name in required:
        if name not in body:
            found.append(f"{schema_name}: missing required field {(prefix + name)!r}")
        elif body[name] is None and "null" not in _types_of(props.get(name, {}), defs):
            found.append(f"{schema_name}: required field {(prefix + name)!r} is null")

    for key, value in body.items():
        prop = props.get(key)
        if prop is None:
            found.append(
                f"{schema_name}: {(prefix + key)!r} is not a declared property "
                f"(billing would ignore it; declared: {sorted(props)})"
            )
            continue
        _check_value(schema_name, prefix + key, value, prop, defs, found)


def violations(schema_name: str, body: dict[str, Any]) -> list[str]:
    """Every way ``body`` fails billing's ``schema_name``, as readable strings.

    Checks, in order: that the schema is one this guard can fully validate
    (``UncheckableSchemaError`` otherwise — see that class for why unknown keywords
    are loud rather than skipped); required fields present and non-null; no
    undeclared properties (a typo'd key is dropped by pydantic and the real
    field then reads as missing); type of every present value; and every
    numeric / length / pattern / format / enum constraint billing declares,
    recursively into nested objects and array items — a body that is
    structurally right but violates ``gt=0`` still 422s in production, so it
    counts.
    """
    contract = load_contract()
    defs = contract["schemas"]
    spec = defs[schema_name] if schema_name in defs else schema(schema_name)

    _assert_checkable(schema_name, spec, defs)

    found: list[str] = []
    _check_object(schema_name, body, spec, defs, found)
    return found


def assert_satisfies(schema_name: str, body: dict[str, Any]) -> None:
    """Hard assertion: ``body`` would be accepted by billing's ``schema_name``."""
    found = violations(schema_name, body)
    assert not found, (
        f"outgoing body does not satisfy billing's {schema_name} "
        f"(vendored from {load_contract()['_meta']['source_path']} @ "
        f"{load_contract()['_meta']['source_commit'][:12]}):\n  - "
        + "\n  - ".join(found)
        + f"\n  body sent: {body!r}"
    )
