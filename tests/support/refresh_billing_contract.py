"""Refresh tests/support/billing_contract.json from the billing_service repo.

Run with a sibling ``billing_service`` (or ``billing-service``) checkout present,
or with ``BILLING_SERVICE_REPO=<path to a billing-service checkout>``:

    python3 tests/support/refresh_billing_contract.py

Only the schemas licenses actually POSTs (plus what they ``$ref``) are vendored;
see ``billing_contract.py`` for why a vendored copy rather than a relative-path
read. ``test_billing_contract_copy_matches_source`` fails when this copy has
drifted from a locally present source, which is the signal to run this.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

# This module is loaded BOTH ways: pytest imports it as a package member
# (``from tests.support.refresh_billing_contract import VENDORED``), and an
# operator runs it as a bare script (``python3 tests/support/…``), where
# ``tests`` is not on the path.
#
# Try the package import FIRST. A bare ``sys.path`` insert would succeed under
# pytest too and bind a SECOND, top-level ``billing_contract`` module object —
# same file, different identity, its own module state — so a test that
# monkeypatched ``tests.support.billing_contract`` would silently not affect the
# copy this module holds. Preferring the package path keeps one module object
# whenever one is possible.
try:
    from tests.support.billing_contract import schema_digest, source_openapi_path, source_repo_path
except ImportError:  # bare-script invocation: put this file's own dir on the path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from billing_contract import schema_digest, source_openapi_path, source_repo_path

CONTRACT_PATH = Path(__file__).with_name("billing_contract.json")

# Hand-pinned, and therefore only ever correct by coincidence unless checked:
# ``_missing_ref_targets`` below refuses to write a set that is not CLOSED under
# ``$ref``. A dangling ``$ref`` is not a cosmetic gap — the guard resolves the
# ref to decide what types a field accepts, so an unresolvable target used to
# mean "accepts anything, silently". ``BudgetPeriod`` is here only because
# ``TokenBudgetCreate.period`` refs it; the next such field must be added too,
# and this script (plus test_vendored_schema_set_is_closed_under_ref) is what
# forces that instead of letting the scope quietly shrink.
VENDORED = (
    "QuotaConfigRequest",
    "TokenBudgetCreate",
    "BudgetPeriod",
    # The offering bodies. Vendored so the hand-built markup / access-charge
    # POSTs get the same 422-proofing the budget bodies already have — nothing
    # in this repo imports billing's models, so a field billing requires and
    # licenses omits is a deterministic 422 no mock can see.
    "MarkupRuleCreate",
    "AccessChargeCreate",
)

# The billing routes whose AUTHORIZATION this repo's docstrings describe, as
# ``(router module, route path)``. The router's own ``prefix=`` is read from the
# source, so the key written into the contract is the real wire path.
#
# Why this exists at all: the gate on a route is not in ``openapi.json``.
# ``main._provision_offering_markup`` / ``_provision_access_charge`` therefore
# carried a HAND-WRITTEN claim about it, and that claim was wrong twice in one
# day — first "``require_pricing_admin`` accepts ``metering-internal``", then a
# retraction asserting ``require_admin``/``license-admin``, which billing #74
# superseded the SAME afternoon with ``require_pricing_writer``. Prose about
# another repo decays faster than anyone re-reads it, so it is derived.
_GATED_ROUTES = (
    ("src/routers/config.py", "/markup-rules"),
    ("src/routers/config.py", "/access-charges"),
)

#: Where the dependency functions named by those routes are defined.
_AUTH_DEPS_MODULE = "src/auth_deps.py"


def find_source() -> Path | None:
    """The live billing openapi.json, if a sibling checkout exists.

    Delegates to ``billing_contract.source_openapi_path`` rather than keeping a
    second copy of the search. The two MUST agree: the test uses that one to
    decide whether it can diff, and this script uses it to decide what to write.
    When they disagreed, the freshness check found a drift the refresh tool then
    refused to fix, which is the worst of both.

    The previous version here hard-coded ``parents[2]`` and ``parents[4]``. From
    a ``.worktrees/<slug>`` checkout — the convention this repo actually uses —
    ``parents[4]`` lands on the repo root, not on its parent directory, so the
    sibling was never found and the script reported "clone billing_service beside
    licenses" while billing_service was sitting right there. Counting parents is
    what the sibling module's docstring already warns against; walking ancestors
    is correct for both layouts and needs no comment about how deep you are.
    """
    return source_openapi_path()


def _router_prefix(tree: ast.Module) -> str:
    """The ``APIRouter(prefix=...)`` value in a router module, or ``""``.

    Read rather than assumed: a hard-coded ``/api/config`` here would keep
    producing a plausible-looking contract key after billing moved the router,
    which is the same class of silently-stale fact this whole derivation exists
    to remove.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "APIRouter"
        ):
            for keyword in node.keywords:
                if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return ""


def _post_route_dependency(tree: ast.Module, path: str) -> str | None:
    """The ``Depends(<name>)`` function name on ``@router.post("<path>", ...)``.

    Returns the name of the LAST such dependency in the signature — billing puts
    the auth dependency last (``session`` then ``_user``) — or None if the route
    declares none, which is itself a finding worth failing on.
    """
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        posts = [
            dec
            for dec in func.decorator_list
            if isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "post"
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and dec.args[0].value == path
        ]
        if not posts:
            continue
        names: list[str] = []
        args = func.args
        for default in (*args.defaults, *args.kw_defaults):
            if (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id == "Depends"
                and default.args
                and isinstance(default.args[0], ast.Name)
            ):
                names.append(default.args[0].id)
        return names[-1] if names else None
    return None


def _string_set_constants(tree: ast.Module) -> dict[str, list[str]]:
    """Every module-level ``NAME = {"a", "b"}`` / ``frozenset({...})`` of strings."""
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and value.args
        ):
            value = value.args[0]
        if not isinstance(value, ast.Set | ast.List | ast.Tuple):
            continue
        constants = [e.value for e in value.elts if isinstance(e, ast.Constant)]
        strings = [e for e in constants if isinstance(e, str)]
        # A set with a non-string member is not a role set — take it whole or
        # not at all, rather than silently keeping the string half of it.
        if strings and len(strings) == len(constants) == len(value.elts):
            found[target.id] = sorted(strings)
    return found


def _dependency_roles(deps_tree: ast.Module, dependency: str) -> list[str]:
    """The role names ``dependency`` admits, resolved through the constant it reads.

    Two hops, both derived: find the dependency function, collect the
    module-level string-set constants its body references, and union them. Going
    straight to ``_PRICING_WRITER_ROLES`` by name would re-introduce exactly the
    coupling this replaces — billing renaming or splitting that constant would
    leave a stale role list here with nothing to notice.
    """
    for func in ast.walk(deps_tree):
        if isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef) and func.name == dependency:
            constants = _string_set_constants(deps_tree)
            used = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)} & set(constants)
            roles: set[str] = set()
            for name in used:
                roles |= set(constants[name])
            return sorted(roles)
    return []


def derive_write_gates(repo: Path) -> dict[str, dict[str, object]]:
    """``{"POST <path>": {"dependency": ..., "roles": [...]}}`` read from billing's source.

    Raises ``ValueError`` rather than returning a partial map: a gate that
    silently resolved to zero roles would make the docstring assertion in
    ``tests/unit/test_provision_docstrings.py`` vacuously satisfiable.
    """
    deps_tree = ast.parse((repo / _AUTH_DEPS_MODULE).read_text(encoding="utf-8"))
    gates: dict[str, dict[str, object]] = {}
    for module, path in _GATED_ROUTES:
        tree = ast.parse((repo / module).read_text(encoding="utf-8"))
        wire_path = _router_prefix(tree) + path
        dependency = _post_route_dependency(tree, path)
        if dependency is None:
            raise ValueError(f"POST {wire_path} in {module} declares no Depends(...) gate")
        roles = _dependency_roles(deps_tree, dependency)
        if not roles:
            raise ValueError(
                f"could not resolve any role names for {dependency}() in {_AUTH_DEPS_MODULE} "
                f"(gate for POST {wire_path}) — a role set that reads as empty would make "
                "the docstring assertion in tests/unit/test_provision_docstrings.py vacuous"
            )
        gates[f"POST {wire_path}"] = {"dependency": dependency, "roles": roles}
    return gates


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError, OSError:
        return "unknown"


def _iter_refs(node: object) -> Iterator[str]:
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


def _missing_ref_targets(schemas: dict[str, object], vendored: Sequence[str]) -> list[str]:
    """Names the vendored set ``$ref``s transitively but does not itself contain.

    Computed as a fixpoint against the SOURCE spec, so adding one schema that
    refs two others reports both rather than one round at a time.
    """
    selected = set(vendored)
    while True:
        refs = set(_iter_refs({name: schemas[name] for name in selected if name in schemas}))
        new = refs - selected
        if not new:
            return sorted(refs - set(vendored))
        selected |= new


def main() -> int:
    source = find_source()
    if source is None:
        print(
            "billing_service/docs/openapi.json not found next to this checkout; "
            "clone nonagenticai/billing_service beside `licenses` and re-run.",
            file=sys.stderr,
        )
        return 2

    spec = json.loads(source.read_text())
    schemas = spec["components"]["schemas"]
    missing = [name for name in VENDORED if name not in schemas]
    if missing:
        print(f"schemas absent from source: {missing}", file=sys.stderr)
        return 2

    # Refuse to write a NON-CLOSED vendored set. Without this the hand-list
    # silently undercounts the moment billing turns a field into a nested model
    # or an enum, and the resulting dangling $ref makes that field accept any
    # value at all.
    dangling = _missing_ref_targets(schemas, VENDORED)
    if dangling:
        print(
            f"VENDORED is not closed under $ref — add {dangling} to VENDORED "
            "in this file and re-run (a dangling $ref makes the field pointing "
            "at it accept ANY value in tests/support/billing_contract.py).",
            file=sys.stderr,
        )
        return 2

    repo = source_repo_path()
    assert repo is not None  # source was found, so its repo root is too
    existing = json.loads(CONTRACT_PATH.read_text())
    meta = dict(existing["_meta"])
    meta.update(
        {
            "source_commit": _git(repo, "log", "-1", "--format=%H", "--", "docs/openapi.json"),
            "source_commit_date": _git(
                repo, "log", "-1", "--format=%ad", "--date=short", "--", "docs/openapi.json"
            ),
            "openapi_info_version": spec["info"]["version"],
            "schemas_vendored": list(VENDORED),
            # The handshake value: billing-service pins these same digests in
            # docs/consumer_contracts/licenses.json and its CI fails when one of
            # these schemas changes. Copy them there in the same change.
            # A list of {schema, sha256} rather than {name: digest}: a schema
            # NAMED "TokenBudgetCreate" keyed to a 64-char hex value reads as a
            # credential to tests/unit/test_no_committed_credentials.py.
            "schema_sha256": [
                {"schema": name, "sha256": schema_digest(schemas[name])}
                for name in sorted(VENDORED)
            ],
        }
    )
    gates = derive_write_gates(repo)
    # Pinned by billing-service as write_gates_sha256, which its CI re-derives
    # from the live router on every run.
    meta["write_gates_sha256"] = schema_digest(gates)
    payload = {
        "_meta": meta,
        "schemas": {name: schemas[name] for name in VENDORED},
        "write_gates": gates,
    }
    CONTRACT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"refreshed {CONTRACT_PATH} from {source} @ {meta['source_commit'][:12]}")
    print(
        "now set schema_sha256 in billing-service docs/consumer_contracts/licenses.json to:\n"
        + json.dumps(meta["schema_sha256"], indent=2, sort_keys=True)
        + f"\nand write_gates_sha256 to: {meta['write_gates_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
