"""Migration 031 stops 34 service tenants claiming they will bring their own key.

WHY THIS FILE EXISTS. `tenants.byo_mode = 'byo_required'` means, per models.py:890,
"commercial: must bring their own provider; worker_management hard-fails a run with
no configured one". A `kind='service'` row is a machine identity — an `svc-*`
principal minted by scripts/provision_service_token.py for one of our own backends.
It has no account and nowhere to have put an API key, so `byo_required` on it is not
a strict policy, it is an unsatisfiable one: the only reachable outcome is the 402.

Measured on prod 2026-09-05: 34 of the 43 service tenants held `byo_required`; the
other 9 held `local_allowed`, with zero counter-examples. That makes 031 a
transcription onto a value its own siblings already carry, not a judgement call.

These tests pin the RULES, not the answers:

* **It names its 34 rows by id, and only those.** Not `WHERE kind='service'` — a
  predicate would also sweep rows created after 2026-09-05 and make the downgrade
  set unknowable. Same "name the rows, never an else-branch" rule 027 and 030 state.
* **By ID, not by name.** Tenants get renamed in this corpus (024 and 026 both do
  it), so `name` is not a stable key between writing the file and running it.
* **It never overwrites an operator.** The UPDATE is guarded
  `WHERE byo_mode = 'byo_required'`, so a re-run changes 0 rows and a value someone
  has since chosen is theirs.
* **It touches none of the 9 already-correct service tenants.** 7 of those were
  corrected by hand on 2026-07-23 for exactly this reason; re-arming them would
  re-arm the 402 that correction was made to stop.
* **It adds no CHECK constraint.** A `CHECK (NOT (kind='service' AND
  byo_mode='byo_required'))` would fail the live provisioning path — the column's
  server_default IS 'byo_required' and `ensure_tenant()` cannot override it — and
  would forbid a legitimate future reclassification. The durable control is the
  generator fix, not a constraint that fails closed on the wrong actor.
* **It fails loudly, never fatally.** A raise out of `_run_db_bootstrap` makes the
  pod refuse to serve, and this service backs ext_authz for the whole platform at
  replicas: 2. A WARNING cannot crashloop it; an EXCEPTION can.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "031_service_tenants_are_not_byo.sql"
DOWN = REPO / "migrations" / "downgrades" / "031_service_tenants_are_not_byo.sql"

#: The 34 rows measured on prod 2026-09-05 as kind='service' AND byo_mode='byo_required'.
#: Pinned so that widening or narrowing the migration has to edit this list too.
THE_THIRTY_FOUR = frozenset(
    {
        "svc-coding-ai",
        "svc-coding-analysis",
        "svc-coding-containers",
        "svc-coding-gateway",
        "svc-coding-github",
        "svc-coding-realtime",
        "svc-coding-repo-jobs",
        "svc-coding-security",
        "svc-coding-testing",
        "svc-fr-backend",
        "svc-fr-backend-kag",
        "svc-hirag-graph",
        "svc-image-e2e-customer",
        "svc-image-gateway",
        "svc-ingestion",
        "svc-ingestion-graph",
        "svc-kag",
        "svc-kag-eval-r46",
        "svc-kag-graph-reconciler",
        "svc-kag-karma",
        "svc-kag-orchestrator-graph",
        "svc-modality-gpuproof-e2e",
        "svc-modality-gw-e2e",
        "svc-modality-phaseb-e2e",
        "svc-n8n-aa",
        "svc-nlp-graph",
        "svc-probe-gwflip-20260813",
        "svc-probe-tenant-header-20260812",
        "svc-probe-tenant-header-20260812b",
        "svc-probe-vouch-20260813",
        "svc-requirements",
        "svc-scientific-processor-graph",
        "svc-vlm-smoke",
        "svc-w2v-shared",
    }
)

#: The 9 service tenants that were ALREADY `local_allowed` on 2026-09-05. 031 does
#: not own them and must never name them — 7 were corrected by hand on 2026-07-23
#: and a downgrade that reverted them would re-arm the 402 that fix removed.
#: Keyed by UUID because the names overlap by prefix (`svc-kag-graph` is a prefix of
#: `svc-kag-graph-reconciler`, `svc-hirag` of `svc-hirag-graph`), so a substring test
#: would be wrong in both directions.
ALREADY_CORRECT: dict[str, str] = {
    "97d33e0a-fc56-5ad6-8c32-1ccc565ff327": "svc-cdmsfirst-n8n",
    "f5e6212e-5a16-5a16-9864-0ce1ab1a08ee": "svc-discovery-engine",
    "9ec0a01d-78cd-54e1-9f40-8e48e93f6707": "svc-hirag",
    "a8035a90-4210-5900-9529-45b3b90b72a2": "svc-kag-graph",
    "e929ef57-5670-5c26-85ac-eab725866516": "svc-karma",
    "bbdb4939-4c4c-5b4c-8286-3ffd3921b8a7": "svc-media-discovery",
    "c13dfe28-f5af-5656-8786-51f34b2629ec": "svc-nlp-service",
    "16f5fb5b-141d-5fc1-b513-24ca7d4a31b4": "svc-scientific-processor",
    "333c001e-03eb-5508-901c-b05409b0df85": "svc-tts-synth",
}

#: ck_tenants_byo_mode (migration 021). A value outside this set cannot be written.
BYO_VOCABULARY = frozenset({"local_allowed", "byo_required", "platform_only"})

_UUID = re.compile(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'")
_NAME_COMMENT = re.compile(r"--\s+(svc-[a-z0-9-]+)")


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _down() -> str:
    return DOWN.read_text(encoding="utf-8")


def _up_body() -> str:
    """The executable part only — the header is prose and mentions many names."""
    return _up().split("DO $$", 1)[1]


def _id_list(sql: str) -> list[str]:
    """The uuids inside the `id IN (...)` list."""
    block = sql.split("id IN (", 1)[1].split(");", 1)[0]
    return _UUID.findall(block)


def _named_rows(sql: str) -> list[str]:
    block = sql.split("id IN (", 1)[1].split(");", 1)[0]
    return _NAME_COMMENT.findall(block)


class TestBothFilesExist:
    def test_the_upgrade_exists(self):
        assert UP.is_file(), f"missing {UP}"

    def test_it_has_a_matching_downgrade(self):
        """Repo convention: every reversible migration ships migrations/downgrades/<same name>."""
        assert DOWN.is_file(), f"missing {DOWN}"
        assert DOWN.name == UP.name


class TestItNamesExactlyTheThirtyFourMeasuredRows:
    def test_the_upgrade_names_thirty_four_ids(self):
        assert len(_id_list(_up())) == 34

    def test_no_id_is_listed_twice(self):
        ids = _id_list(_up())
        assert len(set(ids)) == len(ids), "a duplicated uuid means the list was hand-edited"

    def test_every_id_is_annotated_with_its_tenant_name(self):
        """An anonymous uuid list is unreviewable — a reader cannot tell what it hits."""
        assert len(_named_rows(_up())) == 34

    def test_it_targets_the_measured_population(self):
        assert set(_named_rows(_up())) == THE_THIRTY_FOUR


class TestItUsesTheImmutableKey:
    def test_the_update_selects_on_id_not_on_name(self):
        """024 and 026 both RENAME registry rows, so `name` is not stable across the
        window between writing this file and running it. `id` is the primary key."""
        update = _up_body().split("UPDATE tenants", 1)[1].split(";", 1)[0]
        assert "id IN (" in update
        assert "name =" not in update and "name IN" not in update and "name LIKE" not in update

    def test_it_uses_no_pattern_match_at_all(self):
        """A `name LIKE 'svc-%'` would sweep tenants minted after 2026-09-05."""
        assert "LIKE" not in _up_body()


class TestItNeverOverwritesAnOperator:
    def test_the_update_is_guarded_on_the_prior_value(self):
        update = _up_body().split("UPDATE tenants", 1)[1].split(";", 1)[0]
        assert "byo_mode = 'byo_required'" in update, (
            "the UPDATE must be guarded WHERE byo_mode = 'byo_required' — once an "
            "operator has chosen a value it is theirs, not this file's (027, 030)"
        )

    def test_a_rerun_changes_nothing(self):
        """Guarded on the prior value, the statement is idempotent by construction."""
        assert _up_body().count("UPDATE tenants") == 1

    def test_the_update_does_not_select_on_kind(self):
        """`WHERE kind='service'` would sweep rows created after the measurement and
        make the downgrade set unknowable. `kind` belongs only in the report below."""
        update = _up_body().split("UPDATE tenants", 1)[1].split(";", 1)[0]
        assert "kind" not in update


class TestItWritesOneKnownGoodValue:
    def test_it_writes_only_local_allowed(self):
        assert set(re.findall(r"SET byo_mode = '(\w+)'", _up())) == {"local_allowed"}

    def test_that_value_is_in_the_check_constraint(self):
        """ck_tenants_byo_mode (021) — so the write cannot fail on a constraint."""
        assert "local_allowed" in BYO_VOCABULARY

    def test_it_sets_no_other_column(self):
        assert set(re.findall(r"SET (\w+) =", _up())) == {"byo_mode"}

    def test_it_changes_no_name_no_id_no_kind(self):
        for forbidden in ("SET name", "SET id", "SET kind", "SET cost_centre"):
            assert forbidden not in _up()


class TestItIsDataOnlyAndCannotTakeAuthzDown:
    @pytest.mark.parametrize("forbidden", ["INSERT INTO", "DELETE FROM", "DROP ", "ALTER TABLE"])
    def test_it_contains_no_schema_or_row_lifecycle_statement(self, forbidden: str):
        assert forbidden not in _up_body(), (
            f"031 is a data migration; it must not contain {forbidden!r}"
        )

    def test_it_touches_no_licence(self):
        assert "license" not in _up_body()

    def test_drift_is_a_warning_not_an_exception(self):
        """A raise out of _run_db_bootstrap makes the pod refuse to serve, and this
        service backs ext_authz for the whole platform at replicas: 2."""
        assert "RAISE WARNING" in _up()
        assert "RAISE EXCEPTION" not in _up()

    def test_it_pins_the_row_count_it_measured(self):
        assert "n_fixed <> 34" in _up()


class TestItAddsNoCheckConstraint:
    def test_no_constraint_is_added(self):
        """A CHECK (NOT (kind='service' AND byo_mode='byo_required')) would break the
        live provisioning path: the column's server_default IS 'byo_required' and
        ensure_tenant() cannot override it, so every future service-token mint would
        violate it — against the ext_authz backing store."""
        assert "ADD CONSTRAINT" not in _up()
        assert "CHECK (" not in _up_body()


class TestItReportsTheGeneratorInsteadOfSweepingIt:
    def test_it_counts_service_rows_still_byo_required(self):
        """The root cause is upstream (models.py:896 server_default +
        provision_service_token.py:243). This file must surface a row the generator
        produces after 2026-09-05, not silently mop it up."""
        assert "kind = 'service' AND byo_mode = 'byo_required'" in _up_body()

    def test_the_leak_report_is_a_warning(self):
        assert _up_body().count("RAISE WARNING") >= 2

    def test_the_report_names_the_upstream_fix(self):
        assert "ensure_tenant" in _up_body()


class TestItLeavesTheNineAlreadyCorrectTenantsAlone:
    @pytest.mark.parametrize("uuid,name", sorted(ALREADY_CORRECT.items(), key=lambda kv: kv[1]))
    def test_the_upgrade_never_names_it(self, uuid: str, name: str):
        assert uuid not in _up(), f"031 names {name} ({uuid}), which was already local_allowed"

    @pytest.mark.parametrize("uuid,name", sorted(ALREADY_CORRECT.items(), key=lambda kv: kv[1]))
    def test_the_downgrade_never_names_it(self, uuid: str, name: str):
        """A downgrade that reverted these would re-arm the 402 the 2026-07-23
        hand-correction removed."""
        assert uuid not in _down(), f"the downgrade names {name} ({uuid}), which 031 never owned"


class TestTheDowngradeIsExactAndScopedTwice:
    def test_it_reverts_exactly_the_rows_the_upgrade_set(self):
        assert _id_list(_down()) == _id_list(_up()), (
            "the downgrade's id list must be identical to the upgrade's, in the same "
            "order — anything else means one of the two was edited alone"
        )

    def test_it_restores_the_exact_prior_value(self):
        """All 34 held 'byo_required' before 031 — that is the predicate 031 selected
        them with — so one value restores the exact prior state."""
        assert set(re.findall(r"SET byo_mode = '(\w+)'", _down())) == {"byo_required"}

    def test_it_is_scoped_by_the_value_031_wrote(self):
        """If an operator has since moved one to 'platform_only', that value is theirs."""
        assert "byo_mode = 'local_allowed'" in _down()

    def test_it_never_selects_a_whole_population(self):
        for forbidden in ("kind =", "IS NOT NULL", "LIKE"):
            assert forbidden not in _down().split("UPDATE tenants", 1)[1], (
                f"the downgrade must name its rows, never select on {forbidden!r}"
            )

    def test_it_is_a_single_statement_touching_one_column(self):
        assert _down().count("UPDATE tenants") == 1
        assert set(re.findall(r"SET (\w+) =", _down())) == {"byo_mode"}
