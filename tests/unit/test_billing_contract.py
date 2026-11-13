"""Tests for the billing contract guard itself.

The guard (``tests/support/billing_contract.py``) is what makes the outgoing
``_provision_budget`` bodies checkable against billing's real schemas. A guard
is only worth its line count if it can be shown to FAIL on the thing it exists
to catch, so every check here feeds it a synthetic-but-realistic bad body and
asserts the specific violation — including a required field that does not exist
today, which is the "billing adds a new required field tomorrow" case.
"""

from __future__ import annotations

import copy
import json

import pytest

from tests.support import billing_contract
from tests.support.billing_contract import (
    UncheckableSchemaError,
    assert_ref_closure,
    assert_satisfies,
    load_contract,
    recorded_digests,
    schema,
    schema_digest,
    schema_digests,
    source_openapi_path,
    source_pin_path,
    source_repo_path,
    unvendored_refs,
    violations,
    write_gates,
)
from tests.support.refresh_billing_contract import (
    VENDORED,
    _missing_ref_targets,
    derive_write_gates,
)

GOOD_QUOTA_BODY = {
    "license_id": "01000000-0000-7000-8000-0000000000aa",
    "tenant_id": "01000000-0000-7000-8000-000000000001",
    "quota_monthly": 5000,
}

GOOD_BUDGET_BODY = {
    "tenant_id": "01000000-0000-7000-8000-000000000001",
    "license_id": "01000000-0000-7000-8000-0000000000aa",
    "budget_tokens": 42_000,
    "period": "monthly",
    "source": "license_sync",
}


class TestVendoredContract:
    def test_vendored_schemas_are_present_and_shaped(self):
        """The vendored copy must actually contain what the guard reads. A
        missing/empty file would make every ``violations()`` call vacuous."""
        contract = load_contract()
        assert set(contract["schemas"]) >= {
            "QuotaConfigRequest",
            "TokenBudgetCreate",
            "BudgetPeriod",
        }
        assert contract["_meta"]["source_repo"].startswith("billing_service")
        assert len(contract["_meta"]["source_commit"]) == 40

    def test_quota_config_request_requires_tenant_id(self):
        """The load-bearing fact: billing REQUIRES ``tenant_id`` in the BODY.
        FastAPI never consults a header for a body model, so a header-only
        tenant is a deterministic 422."""
        assert "tenant_id" in schema("QuotaConfigRequest")["required"]

    def test_billing_contract_copy_matches_source(self):
        """Freshness, not the guard: when a sibling ``billing_service`` checkout
        exists, the vendored copy must match it byte-for-byte (per schema).

        This is the ONLY check here allowed to skip — and it says exactly why.
        The guard itself never skips, because the schemas are vendored.

        Skipping here is NOT the CI freshness gate. That gate is
        billing-service's ``tests/test_consumer_contract_pins.py``, which runs
        on every billing PR, has no skip path, and fails when a schema vendored
        here changes (its pin must equal ``_meta.schema_sha256``, asserted by
        ``test_recorded_digests_match_the_vendored_schemas`` below on every
        licenses run). Set ``BILLING_SERVICE_REPO`` to force this diff anywhere.
        """
        source = source_openapi_path()
        if source is None:
            pytest.skip(
                "no sibling billing_service checkout found — vendored copy cannot be "
                "diffed against its source here (this is a FRESHNESS check only; the "
                "contract guard itself runs unconditionally from the vendored copy). "
                "Refresh with: python3 tests/support/refresh_billing_contract.py"
            )
        live = json.loads(source.read_text())["components"]["schemas"]
        vendored = load_contract()["schemas"]
        # Precondition, not decoration: every assertion below is inside the
        # loop, so an empty vendored set would report GREEN having diffed
        # nothing — the same vacuous-loop shape fixed on the quota guard test.
        assert set(vendored) == set(VENDORED), (
            f"vendored file holds {sorted(vendored)} but VENDORED pins {sorted(VENDORED)}"
        )
        for name, spec in vendored.items():
            assert name in live, f"{name} disappeared from {source}"
            assert spec == live[name], (
                f"vendored {name} has drifted from {source}; "
                "run python3 tests/support/refresh_billing_contract.py"
            )
        # The other half of the handshake: billing-service's own CI pins these
        # digests and fails when a schema changes. If its pin and this copy
        # disagree, one side moved without the other.
        pin_path = source_pin_path()
        assert pin_path is not None and pin_path.is_file(), (
            f"billing-service checkout at {source.parents[1]} has no {pin_path}; "
            "its CI cannot be guarding this copy"
        )
        pinned = recorded_digests(json.loads(pin_path.read_text())["schema_sha256"])
        assert pinned == schema_digests(), (
            f"{pin_path} pins {pinned} but this vendored copy hashes to "
            f"{schema_digests()}; refresh this copy and move the pin together"
        )

    def test_recorded_digests_match_the_vendored_schemas(self):
        """Always runs, no sibling needed. ``_meta.schema_sha256`` is the value
        billing-service's CI pins (docs/consumer_contracts/licenses.json); a
        vendored schema edited by hand, or refreshed without re-recording the
        digest, breaks the link between this copy and that pin."""
        contract = load_contract()
        recorded = recorded_digests(contract["_meta"]["schema_sha256"])
        assert set(recorded) == set(VENDORED), sorted(recorded)
        assert recorded == schema_digests(contract), (
            "vendored schemas do not hash to _meta.schema_sha256 — never hand-edit "
            "billing_contract.json; run python3 tests/support/refresh_billing_contract.py"
        )

    def test_a_stale_pattern_changes_the_recorded_digest(self):
        """Non-vacuity of the digest link: re-adding ONE of the 8 decimal
        ``pattern`` entries billing dropped must no longer match the record."""
        mutated = copy.deepcopy(load_contract())
        branch = mutated["schemas"]["MarkupRuleCreate"]["properties"]["markup_pct"]["anyOf"][1]
        branch["pattern"] = "^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$"
        recorded = recorded_digests(mutated["_meta"]["schema_sha256"])
        assert schema_digests(mutated) != recorded
        assert {n for n, d in schema_digests(mutated).items() if d != recorded[n]} == {
            "MarkupRuleCreate"
        }

    def test_vendored_write_gates_match_source(self):
        """Freshness for the AUTHORIZATION half of the contract.

        The gate on a route is not in ``openapi.json`` — it is the ``Depends(...)``
        in billing's route source and the role set that dependency reads — so this
        re-derives it from the sibling checkout and diffs. Same skip posture and
        same reason as the schema freshness check above: the vendored copy is what
        the always-run guard (``test_provision_docstrings.py``) reads.
        """
        repo = source_repo_path()
        if repo is None:
            pytest.skip(
                "no sibling billing_service checkout found — the vendored write_gates "
                "cannot be re-derived here (FRESHNESS only; the docstring guard runs "
                "unconditionally from the vendored copy). Refresh with: "
                "python3 tests/support/refresh_billing_contract.py"
            )
        derived = derive_write_gates(repo)
        vendored = write_gates()
        assert vendored == derived, (
            "vendored write_gates has drifted from billing's source "
            f"({repo}); run python3 tests/support/refresh_billing_contract.py"
        )
        pin_path = source_pin_path()
        assert pin_path is not None and pin_path.is_file(), f"{repo} has no {pin_path}"
        pinned = json.loads(pin_path.read_text())["write_gates_sha256"]
        assert pinned == schema_digest(vendored), (
            f"{pin_path} pins write_gates_sha256={pinned} but the vendored gates hash to "
            f"{schema_digest(vendored)}; refresh this copy and move the pin together"
        )

    def test_recorded_write_gates_digest_matches_the_vendored_gates(self):
        """Always runs. ``_meta.write_gates_sha256`` is what billing-service's CI
        pins and re-derives from its live router on every billing run
        (tests/test_consumer_contract_pins.py), so a billing PR that changes who
        may POST markup-rules / access-charges fails there. This keeps the
        vendored gates tied to that recorded value."""
        contract = load_contract()
        assert contract["_meta"]["write_gates_sha256"] == schema_digest(write_gates(contract)), (
            "vendored write_gates no longer hash to _meta.write_gates_sha256 — never "
            "hand-edit billing_contract.json; run python3 tests/support/refresh_billing_contract.py"
        )

    def test_an_extra_admitted_role_changes_the_write_gates_digest(self):
        """Non-vacuity: one more role on one gate must break the recorded link."""
        mutated = copy.deepcopy(load_contract())
        mutated["write_gates"]["POST /api/config/markup-rules"]["roles"].append("billing-internal")
        assert schema_digest(write_gates(mutated)) != mutated["_meta"]["write_gates_sha256"]

    def test_write_gates_derivation_is_not_vacuous(self):
        """The derivation must be shown to actually FIND a dependency and roles.

        A ``derive_write_gates`` that silently returned ``{"...": {"roles": []}}``
        would make every docstring assertion pass by having nothing to assert.
        ``derive_write_gates`` raises on that, and this pins the live shape.
        """
        gates = write_gates()
        assert set(gates) == {
            "POST /api/config/markup-rules",
            "POST /api/config/access-charges",
        }, sorted(gates)
        for route, gate in gates.items():
            assert gate["dependency"].startswith("require_"), f"{route}: {gate}"
            assert len(gate["roles"]) >= 2, f"{route} resolved to {gate['roles']}"


class TestGuardDiscriminatingPower:
    """Each check proves the guard REJECTS a specific bad body."""

    def test_good_bodies_pass(self):
        assert violations("QuotaConfigRequest", GOOD_QUOTA_BODY) == []
        assert violations("TokenBudgetCreate", GOOD_BUDGET_BODY) == []

    def test_missing_required_field_is_caught(self):
        """The exact production defect: quota body without ``tenant_id``."""
        body = {k: v for k, v in GOOD_QUOTA_BODY.items() if k != "tenant_id"}
        found = violations("QuotaConfigRequest", body)
        assert any("missing required field 'tenant_id'" in v for v in found), found
        with pytest.raises(AssertionError, match="tenant_id"):
            assert_satisfies("QuotaConfigRequest", body)

    def test_null_required_field_is_caught(self):
        body = {**GOOD_QUOTA_BODY, "tenant_id": None}
        found = violations("QuotaConfigRequest", body)
        assert any("is null" in v for v in found), found

    def test_undeclared_property_is_caught(self):
        """A typo'd key is dropped by pydantic and the real field then reads as
        missing — so an unknown property is a violation, not a harmless extra."""
        body = {**GOOD_QUOTA_BODY, "tenantId": "x"}
        found = violations("QuotaConfigRequest", body)
        assert any("not a declared property" in v for v in found), found

    def test_wrong_type_is_caught(self):
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "quota_monthly": "5000"})
        assert any("has type string" in v for v in found), found

    def test_bool_does_not_satisfy_integer(self):
        """``isinstance(True, int)`` is True in Python; billing's validator still
        wants a real integer, so the guard must not be fooled."""
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "quota_monthly": True})
        assert any("has type boolean" in v for v in found), found

    def test_numeric_constraint_is_caught(self):
        """Structurally valid but ``ge=0``-violating still 422s in production.

        A NEGATIVE budget is the violation now. It used to be ``0``, because
        billing declared ``budget_tokens: int = Field(gt=0)`` — billing #73
        relaxed that to ``ge=0`` so a block-all budget is representable at all,
        and this assertion is now the ``minimum`` arm rather than the
        ``exclusiveMinimum`` one. Verified against the DEPLOYED billing pod, not
        just the vendored copy: ``TokenBudgetCreate.budget_tokens`` is
        ``{"type": "integer", "minimum": 0.0}``.
        """
        found = violations("TokenBudgetCreate", {**GOOD_BUDGET_BODY, "budget_tokens": -1})
        assert found == ["TokenBudgetCreate: field 'budget_tokens'=-1 violates minimum=0.0"], found

    def test_zero_budget_is_accepted_because_block_all_must_be_representable(self):
        """``budget_tokens=0`` is a BLOCK-ALL budget, not "no budget".

        This is the half the old test asserted backwards. ``main.py`` deliberately
        forwards a plan's ``token_budget_monthly=0`` to billing (the old
        ``budget and budget > 0`` guard treated the falsy 0 as "no budget" and
        skipped the push, so a block-all licence silently behaved as UNLIMITED —
        a money leak). That forward is only correct if billing accepts it, so
        this pins the far side of the contract. NULL still means unlimited and is
        correctly never sent.
        """
        assert violations("TokenBudgetCreate", {**GOOD_BUDGET_BODY, "budget_tokens": 0}) == []

    def test_min_length_is_caught(self):
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "tenant_id": ""})
        assert any("minLength" in v for v in found), found

    def test_enum_is_caught(self):
        """``period`` is a $ref to the BudgetPeriod enum — the guard must follow
        the $ref, otherwise the field is silently unchecked."""
        found = violations("TokenBudgetCreate", {**GOOD_BUDGET_BODY, "period": "yearly"})
        assert any("is not one of" in v for v in found), found

    def test_guard_catches_a_newly_required_field(self, monkeypatch):
        """The whole point of a contract guard rather than a hand-written body
        assertion: if billing ADDS a required field tomorrow, today's passing
        body must start failing.

        Verified by synthesising exactly that change — a field name that appears
        nowhere in either repo — and re-running the guard against the body that
        passes today.
        """
        mutated = copy.deepcopy(load_contract())
        spec = mutated["schemas"]["QuotaConfigRequest"]
        spec["properties"]["billing_cycle_anchor"] = {"type": "string"}
        spec["required"].append("billing_cycle_anchor")
        monkeypatch.setattr(billing_contract, "load_contract", lambda: mutated)

        found = violations("QuotaConfigRequest", GOOD_QUOTA_BODY)
        assert found == ["QuotaConfigRequest: missing required field 'billing_cycle_anchor'"], found


def _patched(monkeypatch, mutate) -> dict:
    """Install a synthetic contract built by ``mutate`` and return it.

    Every check below needs a schema shape billing does not have TODAY — that
    is the point: a guard cannot demonstrate its discriminating power against a
    corpus that never exercises the keyword, so the keyword is synthesised.
    """
    mutated = copy.deepcopy(load_contract())
    mutate(mutated["schemas"])
    monkeypatch.setattr(billing_contract, "load_contract", lambda: mutated)
    return mutated


class TestVendoredScopeIsClosedUnderRef:
    """A hand-listed scope is only ever correct by coincidence unless asserted."""

    def test_vendored_schema_set_is_closed_under_ref(self):
        """Live invariant: nothing vendored $refs anything unvendored."""
        assert unvendored_refs() == []
        assert_ref_closure()

    def test_closure_check_catches_the_real_undercount(self):
        """Non-vacuity against the REAL contract, no synthetic schema needed.

        ``BudgetPeriod`` is vendored ONLY because ``TokenBudgetCreate.period``
        refs it — exactly the kind of entry a hand-list drops. Removing it from
        the pinned tuple must be reported, otherwise the check would pass on
        any list at all.
        """
        schemas = load_contract()["schemas"]
        assert _missing_ref_targets(schemas, VENDORED) == []
        assert _missing_ref_targets(schemas, ("QuotaConfigRequest", "TokenBudgetCreate")) == [
            "BudgetPeriod"
        ]

    def test_dangling_ref_is_loud_not_silently_permissive(self, monkeypatch):
        """The exact hole this closes.

        Before: an unresolvable ``$ref`` made ``_branches`` return ``[]`` and
        ``_types_of`` return ``set()``; since the type check reads
        ``if accepted and actual not in accepted``, an empty ``accepted``
        short-circuits — so the field accepted ANY value with no complaint.
        Reproduced here with the verifier's own payload.
        """
        mutated = _patched(
            monkeypatch,
            lambda s: s["QuotaConfigRequest"]["properties"].__setitem__(
                "billing_meta", {"$ref": "#/components/schemas/BillingMeta"}
            ),
        )
        assert unvendored_refs(mutated) == ["BillingMeta"]

        bad = {**GOOD_QUOTA_BODY, "billing_meta": {"totally": "wrong shape"}}
        with pytest.raises(UncheckableSchemaError, match="BillingMeta"):
            violations("QuotaConfigRequest", bad)


class TestUnimplementedKeywordsAreLoud:
    """Fail-loud beats silent skip: an unchecked keyword is a GUARD defect."""

    def test_unknown_keyword_raises(self, monkeypatch):
        """``allOf`` is not implemented, so it must ERROR rather than pass."""
        _patched(
            monkeypatch,
            lambda s: s["QuotaConfigRequest"]["properties"]["license_id"].__setitem__(
                "allOf", [{"type": "string"}]
            ),
        )
        with pytest.raises(UncheckableSchemaError, match="allOf"):
            violations("QuotaConfigRequest", GOOD_QUOTA_BODY)

    def test_unknown_format_raises(self, monkeypatch):
        """A format the guard cannot parse must not be silently ignored."""
        _patched(
            monkeypatch,
            lambda s: s["QuotaConfigRequest"]["properties"]["tenant_id"].__setitem__(
                "format", "ipv6"
            ),
        )
        with pytest.raises(UncheckableSchemaError, match="ipv6"):
            violations("QuotaConfigRequest", GOOD_QUOTA_BODY)

    def test_unknown_keyword_on_a_nested_schema_also_raises(self, monkeypatch):
        """The checkability walk must reach INSIDE anyOf/properties, not just
        the top level — otherwise a nested unknown keyword stays silent."""

        def mutate(s):
            s["TokenBudgetCreate"]["properties"]["expires_at"]["anyOf"][0]["uniqueItems"] = True

        _patched(monkeypatch, mutate)
        with pytest.raises(UncheckableSchemaError, match="uniqueItems"):
            violations("TokenBudgetCreate", GOOD_BUDGET_BODY)

    def test_every_keyword_in_the_live_contract_is_implemented(self):
        """The whole real contract must be checkable — no silent skips today.

        ⚠️ The loop CANNOT FAIL on an empty schema set: with no names to iterate,
        the body never runs and this test is green having checked nothing. An
        emptied or mis-keyed vendored file would therefore turn the strongest
        check in this file into a no-op, silently. Measured, not argued: with
        ``schemas`` emptied, the pre-fix body below still reported 1 passed,
        while the version with the floor goes RED.

        ``test_vendored_schemas_are_present_and_shaped`` asserts the same floor,
        but a sibling test's assertion does not constrain this one — if that test
        is renamed, deselected, or fails first, this one still has to stand on
        its own. So the floor is asserted here, and the number of schemas
        actually walked is asserted against the number discovered, which is what
        makes "checked nothing" distinguishable from "checked everything".
        """
        names = list(load_contract()["schemas"])
        assert set(names) >= {"QuotaConfigRequest", "TokenBudgetCreate", "BudgetPeriod"}, (
            f"vendored contract lost schemas — this check would be vacuous: {names}"
        )

        checked = 0
        for name in names:
            violations(name, {})  # raises UncheckableSchemaError if anything is unhandled
            checked += 1
        assert checked == len(names) >= 3, f"walked {checked} of {len(names)} schemas"


class TestConstraintsThatUsedToPassUnchecked:
    """``maximum`` / ``maxLength`` / ``pattern`` / ``format`` and nested required
    fields were all accepted silently. Each check asserts BOTH halves — the bad
    value is caught AND the good value still passes — so a checker that simply
    rejects everything cannot masquerade as working."""

    def test_maximum_is_caught(self, monkeypatch):
        _patched(
            monkeypatch,
            lambda s: s["QuotaConfigRequest"]["properties"]["quota_monthly"].__setitem__(
                "maximum", 10_000
            ),
        )
        assert violations("QuotaConfigRequest", GOOD_QUOTA_BODY) == []
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "quota_monthly": 10_001})
        assert found == ["QuotaConfigRequest: field 'quota_monthly'=10001 violates maximum=10000"]

    def test_max_length_is_caught(self, monkeypatch):
        _patched(
            monkeypatch,
            lambda s: s["QuotaConfigRequest"]["properties"]["tenant_id"].__setitem__(
                "maxLength", 36
            ),
        )
        assert violations("QuotaConfigRequest", GOOD_QUOTA_BODY) == []
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "tenant_id": "x" * 37})
        assert any("maxLength=36" in v for v in found), found

    def test_pattern_is_caught(self, monkeypatch):
        _patched(
            monkeypatch,
            lambda s: s["QuotaConfigRequest"]["properties"]["tenant_id"].__setitem__(
                "pattern", r"^[0-9a-f-]+$"
            ),
        )
        assert violations("QuotaConfigRequest", GOOD_QUOTA_BODY) == []
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "tenant_id": "NOT A UUID"})
        assert any("does not match pattern" in v for v in found), found

    def test_date_time_format_is_caught(self):
        """``expires_at``/``starts_at`` really are ``format: date-time`` in the
        live contract — no synthetic schema needed for this one."""
        assert (
            violations(
                "TokenBudgetCreate", {**GOOD_BUDGET_BODY, "expires_at": "2026-08-08T12:00:00Z"}
            )
            == []
        )
        found = violations("TokenBudgetCreate", {**GOOD_BUDGET_BODY, "expires_at": "next tuesday"})
        assert any("is not a valid date-time" in v for v in found), found

    def test_nested_object_missing_its_own_required_field_is_caught(self, monkeypatch):
        """A nested model used to be checked only as far as "it is an object"."""

        def mutate(s):
            s["BillingMeta"] = {
                "type": "object",
                "properties": {"owner": {"type": "string"}, "note": {"type": "string"}},
                "required": ["owner"],
            }
            s["QuotaConfigRequest"]["properties"]["billing_meta"] = {
                "$ref": "#/components/schemas/BillingMeta"
            }

        _patched(monkeypatch, mutate)
        good = {**GOOD_QUOTA_BODY, "billing_meta": {"owner": "platform"}}
        assert violations("QuotaConfigRequest", good) == []

        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "billing_meta": {"note": "x"}})
        assert found == ["QuotaConfigRequest: missing required field 'billing_meta.owner'"], found

    def test_array_items_are_checked(self, monkeypatch):
        def mutate(s):
            s["QuotaConfigRequest"]["properties"]["tags"] = {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 2,
            }

        _patched(monkeypatch, mutate)
        assert violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "tags": ["a", "b"]}) == []
        found = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "tags": ["a", 7]})
        assert any("'tags[1]' has type integer" in v for v in found), found
        too_many = violations("QuotaConfigRequest", {**GOOD_QUOTA_BODY, "tags": ["a", "b", "c"]})
        assert any("violates maxItems=2" in v for v in too_many), too_many
