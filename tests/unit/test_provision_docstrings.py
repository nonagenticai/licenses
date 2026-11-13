"""The S2S provisioning docstrings must match billing's REAL authorization gate.

THE DEFECT THIS EXISTS FOR
--------------------------
``main._provision_offering_markup`` and ``_provision_access_charge`` carried a
hand-written paragraph about who may call billing's two money-config POSTs, and
that paragraph was wrong twice inside a single day:

1. It claimed ``require_pricing_admin`` accepts the licences SA's
   ``metering-internal`` role. ``metering-internal`` had been retired in billing
   on 2026-07-16.
2. The RETRACTION that replaced it asserted ``require_admin`` /
   ``license-admin``-specifically — and billing #74 superseded that the same
   afternoon with ``require_pricing_writer`` (``license-admin`` OR
   ``pricing-writer``).

Both revisions were written by someone who had just read billing's source. The
problem is not carelessness, it is that prose about another repo has no way to
notice when that repo changes. So the fact is DERIVED: the dependency name on
each route and the role set that dependency admits are AST-read out of the
sibling ``billing_service`` checkout by ``tests/support/refresh_billing_contract.py``
and vendored into ``tests/support/billing_contract.json`` — the same mechanism,
and the same "vendored so it can never skip itself green on a CI runner that
checked out one repo" reasoning, as the request-body schemas next to them.

This test ALWAYS runs. The freshness of the vendored copy is a separate,
explicitly-skippable check in ``test_billing_contract.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.support.billing_contract import write_gates

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Which helper docstring speaks for which billing route.
DOCUMENTED_BY = {
    "POST /api/config/markup-rules": "_provision_offering_markup",
    "POST /api/config/access-charges": "_provision_access_charge",
}

#: Claims that were true once and are not true now. Keyed on the STRING rather
#: than on a line number, because the whole failure mode here is text that
#: outlives the code it describes.
#:
#: * ``require_admin`` / ``require_pricing_admin`` — the two dependencies these
#:   docstrings wrongly named. Naming the CURRENT dependency is what the
#:   positive half of this test requires, so a docstring cannot satisfy both
#:   halves by hedging and listing all three.
#: * ``metering-internal`` — retired in billing 2026-07-16; a docstring that
#:   still mentions it sends an operator to grant a role that grants nothing.
#:
#: ⚠️ If billing ever moves these routes BACK onto ``require_admin``, this list
#: and ``test_docstring_names_the_current_dependency`` become mutually
#: unsatisfiable: one would require the docstring to contain the derived
#: dependency name and the other would forbid that same string. That is
#: deliberate — it fails loud, at the moment the assumption breaks, and the fix
#: is to delete the newly-current name from this tuple. Stated here because the
#: symptom (two unrelated-looking tests red at once) does not point at its cause.
FORBIDDEN = ("require_admin", "require_pricing_admin", "metering-internal")

#: Every docstring in this repo that describes billing's money-write gate.
#: ``service_auth`` is here because its MODULE docstring carried the same stale
#: claim as the two helpers — the defect was never confined to ``main.py``.
GATE_DESCRIBING_DOCSTRINGS = (
    ("main.py", "_provision_offering_markup"),
    ("main.py", "_provision_access_charge"),
    ("service_auth.py", None),  # None = the module docstring
)


def _unnamed_roles(text: str, roles: list[str]) -> list[str]:
    """Which of ``roles`` the docstring ``text`` does not name, matched on WORD BOUNDARIES.

    One definition, used by both the live assertion and the synthetic
    discriminating-power checks below, so the second genuinely exercises the
    first rather than a look-alike copy of it.

    ``role in text`` was the obvious spelling and it is wrong twice:

    * It is satisfied by a PREFIX of a role that is present. If billing added a
      role literally named ``pricing``, ``"pricing" in "``pricing-writer``"`` is
      True and the docstring would pass while naming a different role entirely.
      Measured: with the vendored set forced to
      ``["license-admin", "pricing-writer", "pricing"]`` the whole suite stayed
      green.
    * ``"" in <str>`` is **always** True, so an empty role name — a plausible
      output of a derivation bug in ``refresh_billing_contract.py`` — satisfies
      the check unconditionally and takes the non-vacuity floor with it
      (``len([""] + [...]) >= 2`` is perfectly happy).

    So role names are matched with hyphen-aware boundaries (``-`` counts as part
    of a role name, unlike ``\\b``), and a role that is not a non-empty string is
    a loud ``ValueError`` rather than a silent pass — the vendored contract being
    malformed is a defect in the derivation, not evidence about the docstring.
    """
    missing: list[str] = []
    for role in roles:
        if not isinstance(role, str) or not role.strip():
            raise ValueError(
                f"vendored role {role!r} is not a non-empty string; a role that is not a "
                "real name cannot be looked for in a docstring, and an empty one would "
                'match unconditionally ("" in <str> is always True). '
                "Re-run python3 tests/support/refresh_billing_contract.py"
            )
        if re.search(rf"(?<![\w-]){re.escape(role)}(?![\w-])", text) is None:
            missing.append(role)
    return missing


def _docstring(module: str, function: str | None) -> str:
    """The docstring of ``function`` in ``module``, or the module docstring."""
    tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))
    if function is None:
        return ast.get_docstring(tree) or ""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{function} not found in {module}")


class TestProvisionDocstringsMatchBillingsGate:
    def test_the_vendored_gate_is_present_and_non_vacuous(self):
        """Precondition, not decoration.

        Every assertion below loops over ``write_gates()``. An empty or
        mis-shaped map would make all of them green having checked nothing —
        the vacuous-loop shape this repo has already been bitten by twice.
        """
        gates = write_gates()
        assert set(gates) == set(DOCUMENTED_BY), (
            f"vendored write_gates covers {sorted(gates)} but this test documents "
            f"{sorted(DOCUMENTED_BY)} — refresh or update one of them"
        )
        for route, gate in gates.items():
            assert gate["dependency"], f"{route} has no dependency name"
            assert len(gate["roles"]) >= 1, f"{route} resolved to no roles"
            # Every role must be a real, non-empty name. An empty string here
            # would satisfy the containment check unconditionally while still
            # counting towards the length floor above.
            for role in gate["roles"]:
                assert isinstance(role, str) and role.strip(), (
                    f"{route} carries a non-name role {role!r}"
                )

    @pytest.mark.parametrize("route,function", sorted(DOCUMENTED_BY.items()))
    def test_docstring_names_every_role_billing_accepts(self, route: str, function: str):
        """EVERY role, not just one.

        Naming only ``license-admin`` is how the previous revision read, and it
        is actively misleading: it sends whoever is debugging a 403 to grant the
        broad admin role when the narrow ``pricing-writer`` is the intended fix.
        """
        gate = write_gates()[route]
        text = _docstring("main.py", function)
        assert text, f"{function} has no docstring at all"
        missing = _unnamed_roles(text, gate["roles"])
        assert not missing, (
            f"{function}'s docstring does not name {missing}, which billing's "
            f"{gate['dependency']} accepts for {route}. Derived from "
            "tests/support/billing_contract.json — do not hand-edit the role names, "
            "run python3 tests/support/refresh_billing_contract.py"
        )

    @pytest.mark.parametrize("route,function", sorted(DOCUMENTED_BY.items()))
    def test_the_role_check_fires_on_a_role_the_docstring_does_not_name(
        self, route: str, function: str
    ):
        """Discriminating power, measured against a SYNTHETIC gate.

        Against the real contract this check can only ever report green, because
        the docstrings were written to satisfy it — so passing proves nothing
        about whether it can fail. An executed mutation run made that concrete:
        deleting ``pricing-writer`` from the role ENUMERATION in
        ``_provision_offering_markup``'s docstring did NOT turn this class red,
        because the same docstring names the role again a sentence later. The
        docstring is still correct in that state, but the check had not been
        shown to have any power at all.

        So billing is imagined to add a third role whose name appears nowhere in
        this repo, and the same predicate must reject today's docstring.
        """
        real_roles = write_gates()[route]["roles"]
        text = _docstring("main.py", function)
        # The SAME predicate the real check uses, not a copy of it — a
        # re-implementation here would only demonstrate the power of the copy.
        assert _unnamed_roles(text, real_roles) == []
        assert _unnamed_roles(text, [*real_roles, "revenue-operator"]) == ["revenue-operator"], (
            "the role check cannot see a role the docstring does not name — it is vacuous"
        )

    @pytest.mark.parametrize("route,function", sorted(DOCUMENTED_BY.items()))
    def test_a_role_that_is_only_a_prefix_of_a_named_one_is_reported_missing(
        self, route: str, function: str
    ):
        """The hole a plain ``role in text`` left open.

        ``"pricing" in "``pricing-writer``"`` is True, so a genuinely different
        role whose name happens to be a prefix of one that IS named would read as
        documented. Verified against the real docstrings: forcing the vendored set
        to include ``pricing`` used to keep the whole suite green.
        """
        real_roles = write_gates()[route]["roles"]
        text = _docstring("main.py", function)
        assert any(role.startswith("pricing-") for role in real_roles), (
            f"{route} no longer has a hyphenated pricing role — this check needs a new prefix"
        )
        assert _unnamed_roles(text, [*real_roles, "pricing"]) == ["pricing"], (
            "a role name that is merely a PREFIX of a documented one is being accepted"
        )

    def test_an_empty_role_name_is_a_loud_error_not_a_free_pass(self):
        """``"" in <str>`` is always True — a recurring trap in this codebase.

        An empty role reaching the predicate means the derivation produced
        nonsense, and nonsense must not read as "documented".
        """
        text = _docstring("main.py", "_provision_offering_markup")
        with pytest.raises(ValueError, match="non-empty string"):
            _unnamed_roles(text, ["license-admin", ""])
        with pytest.raises(ValueError, match="non-empty string"):
            _unnamed_roles(text, ["   "])

    @pytest.mark.parametrize("route,function", sorted(DOCUMENTED_BY.items()))
    def test_docstring_names_the_current_dependency(self, route: str, function: str):
        gate = write_gates()[route]
        text = _docstring("main.py", function)
        assert gate["dependency"] in text, (
            f"{function}'s docstring does not name billing's actual gate "
            f"{gate['dependency']!r} for {route}"
        )

    @pytest.mark.parametrize("module,function", GATE_DESCRIBING_DOCSTRINGS)
    def test_no_superseded_claim_survives(self, module: str, function: str | None):
        """No retraction blocks either.

        A docstring that says "this used to say X" still puts X in front of the
        next reader, and both previous revisions here WERE retractions of the
        one before. The fact is derivable, so the correct amount of history to
        carry in the docstring is none.
        """
        text = _docstring(module, function)
        assert text, f"{module}:{function or '<module>'} has no docstring"
        stale = [claim for claim in FORBIDDEN if claim in text]
        assert not stale, (
            f"{module}:{function or '<module>'} still asserts {stale}; billing's real "
            f"gate is vendored in tests/support/billing_contract.json"
        )


class TestPublishedRollbackClaim:
    """The route docstring is PUBLISHED — it is the description in ``docs/openapi.json``.

    ``provision_tenant_offering`` claimed "the tenant is never left
    half-provisioned". That is false in the access-charge arm: if the markup rule
    was already accepted and the access-charge write then fails, the markup rule
    stays. billing_service exposes no DELETE on ``/api/config/markup-rules``
    (GET and POST only) and resolves markups by ``(tenant, service_key, …)``
    independently of this service's posture, so the tenant is billed at a markup
    for a provision that reported failure.

    Unreachable while the markup POST 403s — and live the moment the
    ``pricing-writer`` grant lands, which is this branch's whole premise. So the
    claim is asserted against BOTH the source docstring and the shipped spec: the
    spec is the copy an integrator reads, and it is a generated artifact that can
    go stale independently of the source.
    """

    ROUTE = "/tenants/{name_or_id}/offerings/{app_key}/{code}"

    #: Phrasings that promise a clean rollback. Substring-matched on purpose:
    #: this is prose, and the failure mode is a reassuring sentence, not an
    #: identifier.
    OVERCLAIMS = ("never left half-provisioned", "never half-provisioned")

    @staticmethod
    def _published_description() -> str:
        import json

        spec = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        route = spec["paths"][TestPublishedRollbackClaim.ROUTE]["post"]
        return route["description"]

    def test_source_docstring_does_not_promise_a_clean_rollback(self):
        text = _docstring("main.py", "provision_tenant_offering")
        assert text, "provision_tenant_offering lost its docstring"
        found = [claim for claim in self.OVERCLAIMS if claim in text]
        assert not found, (
            f"the route docstring still promises {found}; the access-charge failure arm "
            "leaves an un-removable markup rule behind"
        )

    def test_source_docstring_states_the_markup_rule_survives(self):
        """The negative half alone would be satisfied by saying nothing at all."""
        text = _docstring("main.py", "provision_tenant_offering")
        lowered = text.lower()
        assert "markup rule" in lowered and "stays" in lowered, text
        assert "partial" in lowered, text

    def test_the_published_spec_carries_the_corrected_description(self):
        """The generated artifact, not just the source.

        ``docs/openapi.json`` is regenerated by hand (``scripts/export-openapi.py``)
        and gated for drift by ``test_openapi_drift.py``; this pins the specific
        sentence rather than merely "the file is in sync", so a regeneration that
        happened to re-publish an overclaiming description is caught on its own.
        """
        description = self._published_description()
        found = [claim for claim in self.OVERCLAIMS if claim in description]
        assert not found, f"docs/openapi.json still publishes {found}"
        assert "markup rule" in description.lower(), description
