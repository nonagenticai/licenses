"""The fleet-bundle rotation must not be able to report success on a rotation that did not happen.

WHAT THESE TESTS ARE FOR
------------------------
``scripts/rotate_fleet_license_tokens.py`` destroys live production credentials
for two namespaces. Every interesting failure mode is a SILENT one — a step that
reports green having done nothing:

* the Secret is updated and no pod ever picks it up (``envFrom`` materialises once
  at container start, and both consumer apps then cache the parsed settings for
  the process lifetime), so the rotation is INERT and stays invisible until the
  revoke;
* the post-revocation "the old token is refused" check passes because the old
  token was already dead before the run started — a control with no
  discriminating power;
* the revoke endpoint writes nothing and the incident is closed on a credential
  that is still live.

So the tests below are mostly about ORDER and about NEGATIVES: they assert that
each guard actually fires, and several of them assert that the guard would still
fire on a mutated input — because a check that has only ever been observed green
has not been shown to have any power at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import rotate_fleet_license_tokens as rot  # noqa: E402

TENANT_A = "639c333a-0000-4000-8000-000000000001"
TENANT_B = "639c333a-0000-4000-8000-000000000002"
OLD_A, OLD_B = "old-token-aaaa", "old-token-bbbb"
NEW_A, NEW_B = "new-token-aaaa", "new-token-bbbb"

#: Every token string this module puts into a fixture. The no-leak test scans
#: output for all of them, so adding a token here without adding it to a fixture
#: is harmless, but using one in a fixture without listing it here is not.
ALL_FIXTURE_TOKENS = (OLD_A, OLD_B, NEW_A, NEW_B)


class FakeClient:
    """A licences API that records call ORDER, because order is the thing at risk."""

    def __init__(
        self,
        *,
        active: dict[str, bool] | None = None,
        licences: dict[str, str] | None = None,
        tenants: dict[str, str] | None = None,
        mint_returns: dict[str, str] | None = None,
        revoke_actually_revokes: bool = True,
    ):
        self.active = dict(active or {OLD_A: True, OLD_B: True})
        self.licences = dict(
            licences or {OLD_A: "lic-a", OLD_B: "lic-b", NEW_A: "lic-a", NEW_B: "lic-b"}
        )
        self.tenants = dict(
            tenants or {OLD_A: TENANT_A, OLD_B: TENANT_B, NEW_A: TENANT_A, NEW_B: TENANT_B}
        )
        self.mint_returns = dict(mint_returns or {"lic-a": NEW_A, "lic-b": NEW_B})
        self.revoke_actually_revokes = revoke_actually_revokes
        self.calls: list[tuple[str, str]] = []

    def introspect(self, raw_token: str) -> dict[str, object]:
        self.calls.append(("introspect", raw_token))
        return {
            "active": self.active.get(raw_token, False),
            "tenant_id": self.tenants.get(raw_token),
            "license_id": self.licences.get(raw_token),
            "app": {"key": "w2v"},
            "reason": None if self.active.get(raw_token) else "token_revoked",
        }

    def mint(self, license_id, tenant_id, token_type, expires_days, long_lived=None):  # noqa: ANN001
        self.calls.append(("mint", license_id))
        new = self.mint_returns[license_id]
        # setdefault, not assignment: a test that deliberately stages a minted-but-DEAD
        # token must not have that state silently overwritten by the fake.
        self.active.setdefault(new, True)
        return new

    def revoke(self, raw_token: str, tenant_id: str) -> bool:
        self.calls.append(("revoke", raw_token))
        if self.revoke_actually_revokes:
            self.active[raw_token] = False
            return True
        return True  # claims success, writes nothing — the historical defect


def _rotations() -> list[rot.TenantRotation]:
    return [
        rot.TenantRotation(tenant_id=TENANT_A, old_token=OLD_A),
        rot.TenantRotation(tenant_id=TENANT_B, old_token=OLD_B),
    ]


class TestBundleValidationHappensBeforeAnythingReachesGit:
    """A malformed bundle CrashLoops the consumer at boot, on a 1-replica Recreate Deployment.

    marketing's ``parse_licence_tokens()`` and sales's ``@field_validator`` both
    raise during ``Settings`` construction. The old pod is already gone by then,
    so "we'll notice in the rollout" is not a recovery plan.
    """

    def test_an_empty_bundle_is_refused_not_treated_as_nothing_to_do(self):
        with pytest.raises(rot.RotationError, match="non-empty object"):
            rot.validate_bundle({}, "x")

    def test_a_non_uuid_tenant_key_is_refused(self):
        with pytest.raises(rot.RotationError, match="not a UUID"):
            rot.validate_bundle({"not-a-uuid": OLD_A}, "x")

    def test_a_blank_token_is_refused(self):
        """A blank token would introspect inactive and SATISFY the post-revocation control."""
        with pytest.raises(rot.RotationError, match="non-empty string"):
            rot.validate_bundle({TENANT_A: "   "}, "x")

    def test_a_valid_bundle_passes(self):
        """The non-vacuity floor: without this, every test above could pass on a broken validator."""
        assert rot.validate_bundle({TENANT_A: OLD_A}, "x") == {TENANT_A: OLD_A}


class TestTheOldTokenMustBeProvablyLiveFirst:
    """Otherwise the post-revocation control measures nothing.

    This is the single most important test in the file. The final check asserts
    the old token is REFUSED after revocation. If the old token had already been
    dead when the run started, that check passes having proved nothing — and the
    operator closes a leak that is still open.
    """

    def test_an_already_inactive_current_token_is_a_hard_failure(self):
        client = FakeClient(active={OLD_A: False, OLD_B: True})
        with pytest.raises(rot.RotationError, match="already introspects INACTIVE"):
            rot.resolve_and_prove_live(client, _rotations(), app_key=None)

    def test_the_same_input_passes_when_the_token_is_live(self):
        """Discriminating power: the guard must not be refusing everything."""
        client = FakeClient()
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        assert [r.license_id for r in rotations] == ["lic-a", "lic-b"]

    def test_a_token_that_introspects_for_a_different_tenant_is_refused(self):
        """Minting on the wrong licence is unrecoverable — the real credential is destroyed."""
        client = FakeClient(tenants={OLD_A: TENANT_B, OLD_B: TENANT_B})
        with pytest.raises(rot.RotationError, match="DIFFERENT tenant"):
            rot.resolve_and_prove_live(client, _rotations(), app_key=None)

    def test_the_app_key_assertion_fires_on_a_mismatch(self):
        client = FakeClient()
        with pytest.raises(rot.RotationError, match="belongs to app"):
            rot.resolve_and_prove_live(client, _rotations(), app_key="kag")


class TestMintingIsAnOverlapNotACutover:
    """The premise the whole runbook rests on.

    ``provision_service_token.py --rotate`` revokes every live token and mints the
    replacement in ONE transaction — a hard cutover with no grace window. This
    script deliberately uses ``POST /licenses/{id}/tokens`` instead, which mints
    ALONGSIDE. If that ever stopped being true, the script would be one merge away
    from taking two namespaces down, so it checks rather than assumes.
    """

    def test_it_refuses_if_minting_killed_the_old_token(self):
        class CutoverClient(FakeClient):
            def mint(self, license_id, tenant_id, token_type, expires_days, long_lived=None):  # noqa: ANN001
                new = super().mint(license_id, tenant_id, token_type, expires_days, long_lived)
                for old, lic in self.licences.items():
                    if lic == license_id and old != new:
                        self.active[old] = False
                return new

        client = CutoverClient()
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        with pytest.raises(rot.RotationError, match="hard cutover, not an overlap"):
            rot.mint_replacements(client, rotations, "internal", 365)

    def test_the_overlap_check_passes_on_a_real_overlap(self):
        """Non-vacuity floor for the test above."""
        client = FakeClient()
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        rot.mint_replacements(client, rotations, "internal", 365)
        assert [r.new_token for r in rotations] == [NEW_A, NEW_B]
        assert client.active[OLD_A] and client.active[OLD_B], "old tokens must survive the mint"

    def test_a_newly_minted_token_that_does_not_introspect_active_never_reaches_git(self):
        """This service has shipped a rotation minting a token introspect rejected on first use."""
        client = FakeClient()
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        client.mint_returns["lic-a"] = "dead-token"
        client.active["dead-token"] = False
        client.tenants["dead-token"] = TENANT_A
        with pytest.raises(rot.RotationError, match="does not introspect active"):
            rot.mint_replacements(client, rotations, "internal", 365)


class TestTheRevocationControlCanFail:
    """⛔ A verification that cannot return a negative result verifies nothing."""

    def test_a_revoke_that_writes_nothing_is_caught(self):
        """The historical defect: /license/revoke gated its write on a licence-aware
        ``active`` check, so a leaked token on a lapsed licence answered 'not found'
        and wrote NOTHING. The operator closed the incident; the credential came back
        live on renewal."""
        client = FakeClient(revoke_actually_revokes=False)
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        with pytest.raises(rot.StrandedError, match="LEAK IS NOT CLOSED"):
            rot.revoke_and_prove_dead(client, rotations)
        assert not any(r.old_revoked for r in rotations)

    def test_a_revoke_that_works_passes_and_marks_it(self):
        """Non-vacuity floor: without this, the test above could pass on a broken check."""
        client = FakeClient()
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        rot.revoke_and_prove_dead(client, rotations)
        assert all(r.old_revoked for r in rotations)
        assert client.active[OLD_A] is False

    def test_the_revoke_is_followed_by_an_introspect_of_the_same_token(self):
        """Order, not just outcome: revoking and then NOT re-checking is the defect shape."""
        client = FakeClient()
        rotations = _rotations()
        rot.resolve_and_prove_live(client, rotations, app_key=None)
        client.calls.clear()
        rot.revoke_and_prove_dead(client, rotations)
        assert ("revoke", OLD_A) in client.calls
        revoke_idx = client.calls.index(("revoke", OLD_A))
        assert ("introspect", OLD_A) in client.calls[revoke_idx:], (
            "the old token was revoked and never re-introspected — the control never ran"
        )


class TestInPodVerificationGatesTheRevoke:
    """The step a runbook written from memory always omits."""

    class FakeCluster:
        def __init__(self, digests: dict[str, str]):
            self.digests = digests
            self.probes: list[str] = []

        def in_pod_digest(self, namespace, target, env_var):  # noqa: ANN001
            self.probes.append(target.workload)
            return self.digests[target.workload]

    def test_a_pod_still_holding_the_old_bundle_fails_and_names_the_likely_cause(self):
        bundle = {TENANT_A: NEW_A}
        cluster = self.FakeCluster({"deploy/marketing": "0" * 64})
        with pytest.raises(rot.RotationError, match="never picked up the new bundle"):
            rot.wait_for_consumers(
                cluster,  # type: ignore[arg-type]
                "marketing",
                [rot.RollTarget.parse("deploy/marketing:api")],
                "CONTROL_SERVICE_LICENSE_TOKENS",
                rot.bundle_digest(bundle),
                timeout_seconds=0,
            )

    def test_the_failure_message_says_the_old_tokens_were_not_revoked(self):
        """An operator reading this at 3am must not have to infer the blast radius."""
        cluster = self.FakeCluster({"deploy/marketing": "0" * 64})
        with pytest.raises(rot.RotationError) as exc:
            rot.wait_for_consumers(
                cluster,  # type: ignore[arg-type]
                "marketing",
                [rot.RollTarget.parse("deploy/marketing:api")],
                "K",
                "f" * 64,
                timeout_seconds=0,
            )
        assert "HAVE NOT BEEN REVOKED" in str(exc.value)

    def test_a_pod_holding_the_new_bundle_passes(self):
        """Non-vacuity floor."""
        bundle = {TENANT_A: NEW_A}
        cluster = self.FakeCluster({"deploy/marketing": rot.bundle_digest(bundle)})
        rot.wait_for_consumers(
            cluster,  # type: ignore[arg-type]
            "marketing",
            [rot.RollTarget.parse("deploy/marketing:api")],
            "K",
            rot.bundle_digest(bundle),
            timeout_seconds=0,
        )
        assert cluster.probes == ["deploy/marketing"]

    def test_the_digest_covers_the_whole_bundle_not_one_tenant(self):
        """A per-tenant check would pass on a pod whose bundle is stale for every OTHER tenant."""
        assert rot.bundle_digest({TENANT_A: NEW_A}) != rot.bundle_digest(
            {TENANT_A: NEW_A, TENANT_B: NEW_B}
        )

    def test_the_digest_is_stable_across_key_order(self):
        """Otherwise verification fails for a reason unrelated to the rotation."""
        assert rot.bundle_digest({TENANT_A: NEW_A, TENANT_B: NEW_B}) == rot.bundle_digest(
            {TENANT_B: NEW_B, TENANT_A: NEW_A}
        )


class TestRollTargetCarriesTheContainer:
    """``deploy/marketing`` has an init container (migrate) and an app container (api),
    BOTH taking envFrom: marketing-secrets. A bare exec picks the default one, and
    verifying the wrong container is a check that passes for the wrong reason."""

    def test_the_container_is_parsed(self):
        t = rot.RollTarget.parse("deploy/marketing:api")
        assert (t.workload, t.container, t.name) == ("deploy/marketing", "api", "marketing")

    def test_a_missing_container_is_allowed_but_recorded_as_none(self):
        assert rot.RollTarget.parse("deploy/marketing").container is None

    def test_a_malformed_target_is_refused(self):
        with pytest.raises(rot.RotationError, match="KIND/NAME"):
            rot.RollTarget.parse("marketing")


class TestArgumentGatesRefuseAnInertRotation:
    """Every one of these returns BEFORE any cluster call, so they need no fixtures."""

    BASE = ["--namespace", "marketing", "--secret", "marketing-secrets", "--key", "K"]

    def test_stage_apply_without_a_deployment_file_is_refused(self, capsys):
        code = main_rc([*self.BASE, "--phase", "stage", "--secrets-file", "s.yaml", "--apply"])
        assert code == 2
        assert "INERT" in capsys.readouterr().err

    def test_stage_apply_without_an_expiry_decision_is_refused(self, capsys):
        code = main_rc(
            [
                *self.BASE,
                "--phase",
                "stage",
                "--secrets-file",
                "s.yaml",
                "--deployment-file",
                "d.yaml",
                "--apply",
            ]
        )
        assert code == 2
        assert "explicit expiry decision" in capsys.readouterr().err

    def test_finalize_without_a_previous_ref_is_refused(self, capsys):
        code = main_rc([*self.BASE, "--phase", "finalize", "--secrets-file", "s.yaml"])
        assert code == 2
        assert "--previous-ref" in capsys.readouterr().err

    def test_finalize_without_roll_targets_is_refused(self, capsys):
        """Revoking without an in-pod check is revoking blind."""
        code = main_rc(
            [*self.BASE, "--phase", "finalize", "--secrets-file", "s.yaml", "--previous-ref", "abc"]
        )
        assert code == 2
        assert "verify in-pod before" in capsys.readouterr().err

    def test_stage_without_a_secrets_file_is_refused(self, capsys):
        assert main_rc([*self.BASE, "--phase", "stage"]) == 2
        assert "--secrets-file" in capsys.readouterr().err


def main_rc(argv: list[str]) -> int:
    return rot.main(argv)


class TestNothingEverPrintsATokenValue:
    """⛔ The security premise of the whole script.

    These tokens were exposed into agent transcripts by careless ``env | grep``
    calls. A rotation tool that prints the values it handles has recreated the
    very disclosure it exists to close.
    """

    def test_the_report_prints_fingerprints_and_no_token_values(self, capsys):
        rotations = _rotations()
        for r, new in zip(rotations, (NEW_A, NEW_B), strict=True):
            r.license_id = "lic"
            r.new_token = new
        rot._report(
            rotations, "stage", dry_run=False, targets=[rot.RollTarget.parse("deploy/m:api")]
        )
        out = capsys.readouterr().out
        for token in ALL_FIXTURE_TOKENS:
            assert token not in out, f"the report leaked {token!r}"
        assert rot._fingerprint(OLD_A)[:9] in out, "the report must still be correlatable"

    def test_error_messages_carry_fingerprints_not_values(self):
        client = FakeClient(active={OLD_A: False, OLD_B: True})
        with pytest.raises(rot.RotationError) as exc:
            rot.resolve_and_prove_live(client, _rotations(), app_key=None)
        assert OLD_A not in str(exc.value)
        assert rot._fingerprint(OLD_A) in str(exc.value)

    def test_the_fingerprint_is_not_a_prefix_of_the_token(self):
        """A 12-char prefix of a token_urlsafe(32) secret is ~72 bits of real key material."""
        fp = rot._fingerprint(OLD_A)
        assert not OLD_A.startswith(fp)
        assert len(fp) == 12

    def test_the_leak_scan_would_actually_catch_a_leak(self):
        """Discriminating power for the scan above — otherwise it proves nothing."""
        assert any(token in f"debug: {OLD_A}" for token in ALL_FIXTURE_TOKENS)


class TestTheFluxOwnershipProbeCannotDumpTheSecret:
    """The naive spelling of this probe printed every value in ``marketing-secrets``.

    ``{{index .metadata.annotations "…"}}`` raises ``index of untyped nil`` when the
    map is absent, and kubectl's template ERROR path prints the whole object first.
    Measured live 2026-09-15. In a tool whose premise is that token values are never
    printed, that is the most important line in the file.

    It also checks the right place: on these Secrets Flux stamps a LABEL and
    ``metadata.annotations`` is nil, so an annotations-only probe answers "not
    Flux-managed" — and proceeds to patch a Secret Flux will revert.
    """

    def _query(self) -> str:
        runner = rot.Runner(dry_run=False)

        class Capture(rot.Cluster):
            def __init__(self):
                self._runner = runner
                self._prefix = ["true"]

        Capture().flux_owner("marketing", "marketing-secrets")
        return next(a for a in runner.calls[0] if a.startswith("go-template="))

    def test_both_labels_and_annotations_are_probed(self):
        query = self._query()
        assert ".metadata.labels" in query, "Flux stamps a LABEL on these Secrets"
        assert ".metadata.annotations" in query

    def test_every_map_access_is_guarded_by_with(self):
        """`with` yields nothing for a nil map; `index` ERRORS, and the error dumps the object."""
        query = self._query()
        assert query.count("{{with") == 4, query
        assert "{{index .metadata" not in query, (
            "an unguarded index on a possibly-nil map — this is the shape that dumped "
            "every base64 value in marketing-secrets to a terminal"
        )

    def test_an_empty_result_is_read_as_not_flux_managed_not_as_an_error(self):
        class Empty(rot.Cluster):
            def __init__(self):
                self._runner = rot.Runner(dry_run=False)
                self._prefix = ["true"]

        assert Empty().flux_owner("kube-system", "k3s-serving") is None

    def test_a_no_value_result_is_also_read_as_not_managed(self):
        """go-template prints the literal `<no value>` for some missing-key shapes."""

        class NoValue(rot.Cluster):
            def __init__(self):
                self._runner = rot.Runner(dry_run=False)
                # `sh -c` swallows the kubectl args as positional params and emits
                # only the literal go-template would have printed.
                self._prefix = ["sh", "-c", 'printf "%s" "<no value>"', "_"]

        assert NoValue().flux_owner("x", "y") is None


class TestTheDryRunWritesNothing:
    def test_runner_write_is_skipped_and_recorded_in_dry_run(self):
        runner = rot.Runner(dry_run=True)
        assert runner.write(["false"]) == ""
        assert runner.calls == [["<dry-run-skipped>", "false"]]

    def test_runner_write_executes_when_not_dry_run(self):
        """Non-vacuity floor: the skip above must be the dry-run flag, not a broken method."""
        runner = rot.Runner(dry_run=False)
        assert runner.write(["echo", "hello"]).strip() == "hello"
        assert runner.calls == [["echo", "hello"]]

    def test_a_nonzero_exit_becomes_a_rotation_error(self):
        runner = rot.Runner(dry_run=False)
        with pytest.raises(rot.RotationError):
            runner.read(["sh", "-c", "exit 7"])


class TestSerialisationMatchesWhatThePodWillHash:
    def test_the_serialised_bundle_round_trips(self):
        bundle = {TENANT_B: NEW_B, TENANT_A: NEW_A}
        assert json.loads(rot.serialise_bundle(bundle)) == bundle

    def test_the_digest_is_of_exactly_the_serialised_string(self):
        """The in-pod check hashes what was WRITTEN; if these two spellings ever
        diverged, verification would fail for a reason unrelated to the rotation."""
        import hashlib

        bundle = {TENANT_A: NEW_A}
        assert (
            rot.bundle_digest(bundle)
            == hashlib.sha256(rot.serialise_bundle(bundle).encode()).hexdigest()
        )
