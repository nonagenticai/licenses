"""Fail CI when the ORM schema and the migration corpus drift apart.

WHY THIS GATE IS LOAD-BEARING
-----------------------------
``scripts/apply_migrations.py`` BASELINES: on the first run against a schema that
already has the app tables, it stamps every ``.sql`` file as applied WITHOUT
running it. ``main.py:_run_db_bootstrap`` calls ``create_tables()`` immediately
BEFORE it, and that creates the table the probe looks for — so a brand-new empty
database is indistinguishable from a populated one, and **every migration is
baselined away on a fresh database, permanently.**

That behaviour is deliberate (replaying the corpus over a ``create_all``'d schema
is unsafe — see ``apply_migrations``' docstring). The consequence is that
``models.py`` is the ONLY thing that builds a fresh database, and anything a
migration expresses that the ORM does not is silently absent there forever.

PR #128 found three such divergences BY HAND. This gate measures the whole set
and pins it, so the next one cannot arrive silently.

⛔ **If this test is deleted, muted, or has its pin bumped without reading the
diff, that defect class re-opens in full.** There is nothing else guarding it.

Design notes — i.e. the ways a gate like this normally fails to be a gate:

* It **cannot skip itself green.** A missing snapshot is ``pytest.fail``, never
  ``pytest.skip``; there is no ``importorskip`` and no ``integration`` marker
  (``pyproject`` addopts deselect ``-m 'not integration'``), so it runs in every
  ``pytest tests/`` invocation the CI archetype makes.
* The corpus arm is built **FROM EMPTY**, never on top of ``create_all``. Most
  files are guarded (``IF NOT EXISTS`` / ``pg_constraint`` probes) and simply
  no-op when replayed over an ORM-built schema, so the comparison comes out clean
  and VACUOUS — migration 001's ``DEFAULT 60`` on ``plan.rate_limit_rpm`` being
  the concrete thing that stays invisible. (Guardedness and no-opping are NOT the
  same property and the tempting "15 of 21 are guaranteed to no-op" summary is
  false — 006 is guarded on the COLUMN but its ``ADD CONSTRAINT`` is not. See
  ``scripts/apply_migrations.py``'s docstring for the per-file account.)
* It proves **discriminating power** against SYNTHETIC snapshots
  (``TestComparatorDiscriminatingPower``), because a comparator cannot show it
  detects a divergence by running against a pair that has none.
* It has a **vacuity floor** with EXACT pins, not ``>=`` floors — a floor cannot
  catch the drop it exists for (set it to 0 and everything stays green).
* Both arms are generated **in CI, from the repo**. Neither reads prod. Prod was
  the one-time reference used to decide which arm is "right" per divergence; that
  judgement lives in the committed snapshots, not in a live query.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.schema_diff import comparable, divergence_report, snapshot_object_counts

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "docs" / "schema"
ORM_SNAPSHOT = SNAPSHOT_DIR / "orm.json"
CORPUS_SNAPSHOT = SNAPSHOT_DIR / "migrations.json"
BUILDER = ROOT / "scripts" / "build_schema_arms.py"
SHIM = ROOT / "tests" / "support" / "schema_genesis_shim.sql"
MIGRATIONS_DIR = ROOT / "migrations"

REGENERATE = (
    "uv run python scripts/build_schema_arms.py --dsn <dsn> --mode both --refresh-snapshots"
)

# ---------------------------------------------------------------------------
# EXACT pins. Not ">=" floors — a floor cannot catch the drop it exists for.
# Changing any of these requires a deliberate line edit in the same commit that
# changes the schema, which is exactly the moment a human should be looking.
# ---------------------------------------------------------------------------

# Every place a FRESH database (ORM) and the migration corpus disagree.
# Measured 2026-08-12 against prod as the reference. Lowering this is what
# fixing one looks like; raising it must be justified in the PR body.
#
# 46 -> 15 on 2026-08-12: the 27 missing server_defaults, the redundant
# ix_license_tenant_id, ux_plan_app_code_region's NULL semantics and
# ux_token_license_hash's constraint-vs-index shape were all closed in models.py
# (ORM -> prod, never the reverse).
#
# 15 -> 17 on 2026-09-07 (migration 033 / #188): ck_license_status and
# ck_token_token_type are new CHECK constraints on varchar(n) columns, so their
# CAST TEXT differs between the ORM arm (`(status)::text = ANY (...::varchar[]
# ::text[])`) and the corpus arm (`status = ANY (...::text[])`) for the exact
# same reason the existing 11 text-vs-varchar(n) columns do — same class, not a
# new one. ck_tenants_service_not_byo_required is NOT in this count: its
# predicate compares two varchar columns to each other with `=`, no array CAST
# involved, so both arms produce byte-identical constraint text.
#
# THE REMAINING 17 ARE DELIBERATE, AND EACH HAS A REASON. Do not "fix" one
# without reading this — three of them mean the ORM is RIGHT and the corpus is
# silent, so aligning them would be a regression:
#   * 13 x text-vs-varchar(n) (11 columns + the 2 CHECK constraints above): the
#     ORM is STRICTER than prod. Measured on prod 2026-08-12 (columns) and
#     2026-09-07 (the two new CHECKs, against the same measured vocabulary the
#     constraints themselves enforce): ZERO rows exceed any limit, so a restore
#     into a fresh-built schema is not rejected today. Loosening to Text would
#     be prod -> ORM, the wrong direction.
#   * license.tenant_id NOT NULL: here the CORPUS is the weak one — prod AND the
#     ORM both have it, no migration ever set it. Closing it needs a 022 that is
#     a no-op on every real database; not worth a migration file.
#   * uq_license_active_tenant_app_subject's predicate ((status)::text vs
#     status): a pure consequence of the varchar/text family above.
#   * idx_license_tenant is ONLY-IN-ORM and PROD HAS IT (measured on the primary),
#     while NO .sql file declares it — prod's copy came from create_all back when
#     `license` did not yet exist, which is the only remaining source. The ORM is
#     correct here and the corpus is silent.
#     (ux_token_license_hash was listed alongside it on the same reasoning. That
#     reasoning was FALSE — init.sql:115 DOES declare it, as a bare unique index,
#     and prod carries 0 pg_constraint rows for it — so it was fixed in models.py
#     rather than excused here.)
#   * pgcrypto ONLY-IN-CORPUS: gen_random_uuid() is a PG13+ built-in, so nothing
#     needs the extension.
EXPECTED_DIVERGENCES = 17

# Vacuity backstop: an empty-vs-empty comparison is trivially "clean".
#: 028 adds `bundle` (6 columns) and `bundle_app` (3), so BOTH arms move by
#: +2 tables / +9 columns / +4 indexes / +5 constraints. The indexes are
#: bundle_pkey, bundle_key_key, bundle_app_pkey and idx_bundle_app_app; the
#: constraints are those three keys plus bundle_app's two FKs. That both arms
#: move by the SAME amounts is the property this gate exists to confirm — the
#: migration and models.py describe the same two tables, and if only one arm had
#: moved, that would itself be the divergence.
EXPECTED_ORM_COUNTS = {
    # 11 -> 12: tenant_bundle (migration 039 + models.TenantBundle).
    # 12 -> 16: tenant_owner, api_client, app_issuer, tenant_app_grant (migration 050).
    # 16 -> 17: bundle_ensurer (migration 052 + models.BundleEnsurer).
    # 17 -> 20: app_requires, bundle_caller, tenant_dependency_grant (059 +
    #           models.AppRequires / BundleCaller / TenantDependencyGrant).
    # 20 -> 21: realm_owner (072 + models.RealmOwner).
    "tables": 21,
    "sequences": 0,
    "extensions": 1,
    # 79 -> 80: app.audience (migration 022 + models.App.audience).
    # 80 -> 81: tenants.archived_at (migration 023 + models.Tenant.archived_at).
    # 81 -> 82: tenant_app_byo_mode.offering_code (migration 025 +
    #           models.TenantAppByoMode.offering_code). Nullable, so no constraint.
    # 82 -> 84: tenants.kind + tenants.cost_centre (migration 027 + models.Tenant).
    # 84 -> 93: bundle (6) + bundle_app (3) (migration 028 + models.py).
    # 93 -> 94: subject.tenant_id (migration 029 + models.Subject). Nullable, but
    #           it carries a NAMED FK (fk_subject_tenant) and an explicit index
    #           (idx_subject_tenant), so unlike tenants.archived_at it moves the
    #           index and constraint counts too — in BOTH arms.
    # 94 -> 98: license.deleted_at / deleted_by / delete_reason /
    #           deleted_from_status (migration 036 + models.License). All four are
    #           NULLable with no default, carry no FK and no CHECK, and 036 adds no
    #           index — so ONLY this number moves, in BOTH arms, by the same +4.
    #           ⛔ NO CHECK COUPLING THEM, deliberately: the obvious
    #           "deleted_at IS NULL iff deleted_from_status IS NULL" would be
    #           un-writable during a code-rollback window, so the invariant is
    #           enforced in soft_delete_licenses_for_subject, which sets all four
    #           in one UPDATE. See 036's header.
    # 98 -> 99: token.granted_roles (migration 038 + models.Token). NOT NULL
    #           DEFAULT '{}', no index — its two CHECKs move `constraints` below.
    # 99 -> 103: tenant_bundle (tenant_id, bundle_id, created_at) + license.bundle_id
    #           (migration 039 + models.TenantBundle / License.bundle_id).
    # 103 -> 105: license.token_budget_override + license.quota_override (migration
    #           041 + models.License, #239). NULLable, no index; their two named
    #           CHECKs move `constraints` below.
    # 105 -> 106: tenants.budget_currency (migration 043 + models.Tenant).
    # 106 -> 108: token.long_lived_owner + token.long_lived_reason (migration 044 +
    #           models.Token, licenses#244). NULLable, no index.
    # 108 -> 109: subject.actor_sub (migration 047 + models.Subject). NULLable uuid,
    #           no default, no index, no FK, no CHECK — only this number moves.
    # 109 -> 127: the four 050 tables (3+5+2+6) + tenants.owner_key/external_ref.
    # 127 -> 129: bundle_ensurer (bundle_id, owner_key) (052).
    # 129 -> 141: app_requires (3) + bundle_caller (3) + tenant_dependency_grant (5)
    #           + license.is_dependency (059).
    # 141 -> 144: realm_owner.{realm, owner_key, created_at} (072).
    "columns": 144,
    # 31 -> 30: dropped the duplicate ix_license_tenant_id (index=True on
    # License.tenant_id alongside an explicit Index of the same shape).
    # 30 -> 31: ix_tenants_kind (027 + models.py). The FIRST draft of 027 declared
    # this index in the ORM only; this gate reported it as a new ORM-only
    # divergence, which is exactly what it is for.
    # 31 -> 35: bundle/bundle_app keys + idx_bundle_app_app (028 + models.py).
    # 35 -> 36: idx_subject_tenant (029 + models.py). Declared as an explicit
    #           Index in __table_args__, NOT index=True — index=True would emit a
    #           SECOND identical btree (ix_subject_tenant_id) and this number
    #           would move by 2 against the corpus's 1, which is precisely the
    #           report this gate would then owe you.
    # 36 -> 39: tenant_bundle_pkey, idx_tenant_bundle_bundle, idx_license_bundle (039).
    # +6 (050): 4 new pkeys, uq_tenants_owner_name, uq_tenants_owner_external_ref,
    #   uq_tenant_app_grant_active; minus tenants_name_key (UNIQUE(name) dropped;
    #   ix_tenants_name stays, now non-unique).
    # 45 -> 46: bundle_ensurer_pkey (052).
    # 46 -> 49: app_requires_pkey, bundle_caller_pkey, tenant_dependency_grant_pkey (059).
    # 49 -> 50: realm_owner_pkey (072).
    "indexes": 50,
    # 33 -> 31: ux_plan_app_code_region and ux_token_license_hash both became
    # unique Indexes (prod's shape) instead of UniqueConstraints, so neither
    # counts as a constraint any more.
    # 31 -> 32: ck_app_audience.
    # 32 -> 33: ck_tenants_kind.
    # 33 -> 38: bundle + bundle_app keys and FKs (028 + models.py).
    # 38 -> 39: fk_subject_tenant (029 + models.py). NAMED on both arms on
    #           purpose — unnamed, create_all emits subject_tenant_id_fkey while
    #           029 emits fk_subject_tenant, and the arms would differ by name.
    # 39 -> 42: ck_license_status, ck_token_token_type,
    #           ck_tenants_service_not_byo_required (migration 033 + models.py).
    # 42 -> 43: ck_tenants_cost_centre (migration 034 + models.Tenant).
    # 43 -> 45: ck_token_granted_roles_allowlist + ck_token_granted_roles_short_lived
    #           (migration 038 + models.Token).
    # 45 -> 49: tenant_bundle_pkey, fk_tenant_bundle_tenant, fk_tenant_bundle_bundle,
    #           fk_license_bundle (migration 039). All NAMED on both arms.
    # 49 -> 51: ck_license_token_budget_override_nonneg + ck_license_quota_override_nonneg
    #           (migration 041 + models.License). NAMED on both arms.
    # 51 -> 52: ck_tenants_budget_currency (migration 043 + models.Tenant).
    # 52 -> 53: ck_token_long_lived_registered (migration 044 + models.Token).
    # 53 -> 63 (050): +4 pkeys, ck_tenant_owner_key_format, fk_api_client_owner,
    #   fk_app_issuer_app, fk_app_issuer_owner, fk_tenant_app_grant_tenant,
    #   fk_tenant_app_grant_owner, fk_tenants_owner; minus tenants_name_key.
    # 63 -> 66 (052): bundle_ensurer_pkey, fk_bundle_ensurer_bundle,
    #   fk_bundle_ensurer_owner.
    # 059: app_requires_pkey, ck_app_requires_not_self,
    #   fk_app_requires_app / _required_app / _caller_owner; bundle_caller_pkey,
    #   fk_bundle_caller_bundle / _owner / _app; tenant_dependency_grant_pkey,
    #   fk_tenant_dependency_grant_tenant / _owner / _app;
    #   ck_license_dependency_unbundled (66 -> 80).
    # 80 -> 82: realm_owner_pkey + fk_realm_owner_owner (072).
    "constraints": 82,
}
EXPECTED_CORPUS_COUNTS = {
    # 11 -> 12: tenant_bundle, same as the ORM arm (migration 039).
    # 12 -> 16: tenant_owner, api_client, app_issuer, tenant_app_grant (migration 050).
    # 16 -> 17: bundle_ensurer (migration 052 + models.BundleEnsurer).
    # 17 -> 20: app_requires, bundle_caller, tenant_dependency_grant (059 +
    #           models.AppRequires / BundleCaller / TenantDependencyGrant).
    # 20 -> 21: realm_owner (072 + models.RealmOwner).
    "tables": 21,
    "sequences": 0,
    "extensions": 2,
    # 79 -> 80 -> 81 and 31 -> 32: app.audience + ck_app_audience, then
    # tenants.archived_at (a nullable column, so no new constraint).
    # Both arms move together because 022 and models.py describe the same column;
    # if only ONE of these numbers had needed changing, that would itself have been
    # the divergence this gate exists to report.
    # 81 -> 82: tenant_app_byo_mode.offering_code (migration 025 + models.py),
    # nullable so constraints stay at 32 — both arms move together again.
    # 82 -> 84 / 29 -> 30 / 32 -> 33: tenants.kind + tenants.cost_centre,
    # ix_tenants_kind and ck_tenants_kind (migration 027 + models.Tenant). Both
    # arms move by the same amounts, which is the point — 027 and the ORM
    # describe the same three objects.
    # 84 -> 93 / 30 -> 34 / 33 -> 38: bundle + bundle_app (migration 028 +
    # models.py). Both arms move by identical amounts again.
    # 93 -> 94 / 34 -> 35 / 38 -> 39: subject.tenant_id, idx_subject_tenant and
    # fk_subject_tenant (migration 029 + models.Subject). Both arms move by the
    # same +1/+1/+1, which is the property this gate exists to confirm: 029 and
    # models.py describe the same three objects. Migration 030 is DATA-only (it
    # sets cost_centre on 12 existing rows), so it deliberately moves none of
    # these three numbers — a migration that renamed or added a COLUMN would have
    # had to.
    # 94 -> 98: the same four license soft-delete columns as the ORM arm above
    # (migration 036 + models.License). Both arms move by the identical +4, which
    # is the property this gate exists to confirm; indexes and constraints stay
    # put because 036 adds neither.
    # 98 -> 99: token.granted_roles, same as the ORM arm (migration 038).
    # 99 -> 103: tenant_bundle (3) + license.bundle_id, same as the ORM arm (039).
    # 103 -> 105: the two license override columns, same as the ORM arm (041).
    # 105 -> 106: tenants.budget_currency, same as the ORM arm (043).
    # 106 -> 108: the two long-lived register columns, same as the ORM arm (044).
    # 108 -> 109: subject.actor_sub, same as the ORM arm (047).
    # 109 -> 127: the four 050 tables (3+5+2+6) + tenants.owner_key/external_ref.
    # 127 -> 129: bundle_ensurer (bundle_id, owner_key) (052).
    # 129 -> 141: app_requires (3) + bundle_caller (3) + tenant_dependency_grant (5)
    #           + license.is_dependency (059).
    # 141 -> 144: realm_owner.{realm, owner_key, created_at} (072).
    "columns": 144,
    # 35 -> 38: the same three 039 indexes as the ORM arm.
    # +6 (050): 4 new pkeys, uq_tenants_owner_name, uq_tenants_owner_external_ref,
    #   uq_tenant_app_grant_active; minus tenants_name_key (UNIQUE(name) dropped;
    #   ix_tenants_name stays, now non-unique).
    # 44 -> 45: bundle_ensurer_pkey (052), same as the ORM arm.
    # 45 -> 48: app_requires_pkey, bundle_caller_pkey, tenant_dependency_grant_pkey
    #           (059), same as the ORM arm.
    # 48 -> 49: realm_owner_pkey (072).
    "indexes": 49,
    # 39 -> 42: same three CHECK constraints as the ORM arm above (migration 033).
    # Both arms move by the identical amount, which is the point.
    # 42 -> 43: ck_tenants_cost_centre (migration 034 + models.Tenant). Both arms
    # move by the identical +1, which is the point.
    # 43 -> 45: the two granted_roles CHECKs, same as the ORM arm (migration 038).
    # 45 -> 49: the same four 039 keys/FKs as the ORM arm.
    # 49 -> 51: the same two 041 override CHECKs as the ORM arm.
    # 51 -> 52: ck_tenants_budget_currency, same as the ORM arm (043).
    # 52 -> 53: ck_token_long_lived_registered, same as the ORM arm (044).
    # 53 -> 63 (050): +4 pkeys, ck_tenant_owner_key_format, fk_api_client_owner,
    #   fk_app_issuer_app, fk_app_issuer_owner, fk_tenant_app_grant_tenant,
    #   fk_tenant_app_grant_owner, fk_tenants_owner; minus tenants_name_key.
    # 63 -> 66 (052): bundle_ensurer_pkey, fk_bundle_ensurer_bundle,
    #   fk_bundle_ensurer_owner.
    # 059: app_requires_pkey, ck_app_requires_not_self,
    #   fk_app_requires_app / _required_app / _caller_owner; bundle_caller_pkey,
    #   fk_bundle_caller_bundle / _owner / _app; tenant_dependency_grant_pkey,
    #   fk_tenant_dependency_grant_tenant / _owner / _app;
    #   ck_license_dependency_unbundled (66 -> 80).
    # 80 -> 82: realm_owner_pkey + fk_realm_owner_owner (072).
    "constraints": 82,
}

# The corpus arm must replay EVERY file. A builder that silently skipped one
# would produce a SMALLER divergence set, which reads as an improvement — that
# is enforced by `replayed == on_disk` in the test below, which needs no
# expected number.
#
# ⛔ There used to be an `EXPECTED_MIGRATION_FILES = N` here with ~140 lines of
# per-migration bump justifications. Deleted 2026-10-31 (owner ruling: "remove
# all hardcoded counts"): it was redundant with the on-disk comparison, and it
# made every new migration a manual edit to a comment wall nobody could read.
# 024 and 026 are DATA-only (each renames two tenant rows), so the three
# structural counts above are deliberately unchanged. A migration that renamed a
# COLUMN would have had to move them too.


def _libpq_dsn() -> str:
    """The DSN the arm builder needs (libpq form, not SQLAlchemy's)."""
    from tests.conftest import TEST_DATABASE_URL

    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _load(path: Path) -> dict:
    if not path.exists():
        pytest.fail(
            f"missing committed snapshot {path.relative_to(ROOT)} — regenerate with:\n  {REGENERATE}\n"
            "This is a failure, never a skip: a gate that skips when its input is absent is not a gate."
        )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live_arms() -> dict[str, dict]:
    """Build both arms by invoking the DOCUMENTED command in a subprocess.

    Deliberately a subprocess rather than an import: the gate must not be able to
    pass against a snapshot the documented fix command would not reproduce. Same
    idiom as ``test_openapi_drift.py``.
    """
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--dsn", _libpq_dsn(), "--mode", "both"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(
            "could not build the schema arms — this is a REAL failure, not an environment problem "
            "to skip past. A migration that no longer replays from empty is exactly what this gate "
            f"exists to report.\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    return json.loads(proc.stdout)


@pytest.mark.timeout(300)
class TestSnapshotsMatchTheCode:
    """⚠️ 300s, matching ``live_arms``' own ``subprocess.run(..., timeout=300)``.

    The repo-wide per-test timeout is 60s (``pyproject.toml``), so this fixture
    could never use the budget its author gave it: the subprocess replays every
    migration from empty and builds both schema arms, and pytest shot the test at
    60s while the subprocess was still allowed another four minutes.

    Observed as a CI hang in ``pytest_runtest_setup`` -> ``_fillfixtures`` (i.e.
    inside the fixture, not in any assertion) on two branches, while the same
    commit's tests take **0.93s** locally against an idle scratch database. The
    CI slowness itself is NOT explained here — the ephemeral per-job postgres is
    the obvious suspect and I could not reproduce it — but the 60s guillotine over
    a 300s budget is a genuine mismatch either way, and it is the reason the
    failure surfaces as an unattributable timeout rather than a real verdict.

    If this ever times out at 300s, that IS a finding: treat it as the migration
    corpus no longer replaying, exactly as the fixture's own failure message says.
    """

    def test_orm_snapshot_matches_committed(self, live_arms):
        live = comparable(live_arms["orm"])
        committed = comparable(_load(ORM_SNAPSHOT))
        drift = divergence_report(committed, live)
        assert not drift, (
            "models.py changed without refreshing docs/schema/orm.json.\n"
            + "\n".join(f"  - {d}" for d in drift)
            + f"\n\nRegenerate with:\n  {REGENERATE}"
        )

    def test_migration_snapshot_matches_committed(self, live_arms):
        live = comparable(live_arms["corpus"])
        committed = comparable(_load(CORPUS_SNAPSHOT))
        drift = divergence_report(committed, live)
        assert not drift, (
            "the migration corpus changed without refreshing docs/schema/migrations.json.\n"
            + "\n".join(f"  - {d}" for d in drift)
            + f"\n\nRegenerate with:\n  {REGENERATE}"
        )

    def test_corpus_arm_replayed_every_migration(self, live_arms):
        """A builder that swallowed a failing migration would shrink the
        divergence set and look like an improvement."""
        replayed = live_arms["corpus"]["_provenance"]["migration_files"]
        on_disk = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
        assert replayed == on_disk


class TestDivergenceIsExactlyThePinnedSet:
    def test_divergence_count_is_pinned(self, live_arms):
        report = divergence_report(comparable(live_arms["orm"]), comparable(live_arms["corpus"]))
        assert len(report) == EXPECTED_DIVERGENCES, (
            f"ORM/corpus divergence count changed: {len(report)} vs pinned {EXPECTED_DIVERGENCES}.\n"
            "A NEW divergence means a fresh database silently lacks something the corpus declares "
            "(or vice versa). Read every line before touching the pin.\n"
            + "\n".join(f"  - {d}" for d in report)
        )

    def test_live_divergence_equals_the_committed_divergence(self, live_arms):
        """The pin alone could be satisfied by a DIFFERENT set of the same size."""
        live = divergence_report(comparable(live_arms["orm"]), comparable(live_arms["corpus"]))
        committed = divergence_report(
            comparable(_load(ORM_SNAPSHOT)), comparable(_load(CORPUS_SNAPSHOT))
        )
        assert live == committed, (
            "the SET of divergences changed even though the count may not have.\n"
            + "\n".join(f"  - {d}" for d in sorted(set(live) ^ set(committed)))
        )


class TestGateIsNotVacuous:
    """Without these, an empty or truncated snapshot compares 'clean'."""

    def test_orm_snapshot_object_counts(self):
        assert snapshot_object_counts(comparable(_load(ORM_SNAPSHOT))) == EXPECTED_ORM_COUNTS

    def test_corpus_snapshot_object_counts(self):
        assert snapshot_object_counts(comparable(_load(CORPUS_SNAPSHOT))) == EXPECTED_CORPUS_COUNTS

    def test_identical_snapshots_report_clean(self):
        """Negative control: a permanently-red comparator would read as working."""
        snap = comparable(_load(ORM_SNAPSHOT))
        assert divergence_report(snap, json.loads(json.dumps(snap))) == []


class TestComparatorDiscriminatingPower:
    """Synthetic mutations — a comparator cannot prove it detects a divergence
    by running against a pair that has none."""

    @staticmethod
    def _pair() -> tuple[dict, dict]:
        snap = comparable(_load(ORM_SNAPSHOT))
        return snap, json.loads(json.dumps(snap))

    def test_sees_a_constraint_rename(self):
        """``fk_license_tenant`` vs ``license_tenant_id_fkey`` is the exact
        divergence #128 found by hand; a name-insensitive comparator misses it."""
        left, right = self._pair()
        key = next(iter(right["constraints"]))
        right["constraints"]["renamed." + key.split(".", 1)[1]] = right["constraints"].pop(key)
        report = divergence_report(left, right)
        assert any(key in line for line in report), report

    def test_sees_a_server_default(self):
        """The ``plan.rate_limit_rpm`` DEFAULT 60 case."""
        left, right = self._pair()
        key = next(iter(right["columns"]))
        right["columns"][key] = {**right["columns"][key], "default": "'mutated'::text"}
        report = divergence_report(left, right)
        assert any(key in line and "default" in line for line in report), report

    def test_sees_a_nullability_flip(self):
        """The ``license.tenant_id`` NOT NULL case."""
        left, right = self._pair()
        key = "license.tenant_id"
        right["columns"][key] = {
            **right["columns"][key],
            "not_null": not right["columns"][key]["not_null"],
        }
        report = divergence_report(left, right)
        assert any(key in line and "not_null" in line for line in report), report

    def test_sees_an_index_predicate_change(self):
        """The ``ux_plan_app_code_region`` COALESCE case — same name, same
        columns, different NULL semantics."""
        left, right = self._pair()
        key = "plan.ux_plan_app_code_region"
        right["indexes"][key] = {
            **right["indexes"][key],
            "def": "CREATE UNIQUE INDEX ux_plan_app_code_region ON plan USING btree (app_id, code)",
        }
        report = divergence_report(left, right)
        assert any(key in line and "def" in line for line in report), report

    def test_sees_a_dropped_index(self):
        left, right = self._pair()
        key = "token.idx_token_hash"
        right["indexes"].pop(key)
        assert any(key in line for line in divergence_report(left, right))

    def test_sees_a_missing_extension(self):
        left, right = self._pair()
        right["extensions"] = []
        assert divergence_report(left, right)


class TestTheShimCannotGrow:
    """The genesis shim is the ONE hand-authored input to the corpus arm.
    Growing it is how someone would 'fix' a red corpus arm by hiding a failing
    migration instead of fixing it."""

    def test_the_shim_is_one_statement(self):
        body = "\n".join(
            line
            for line in SHIM.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        statements = [s for s in (s.strip() for s in body.split(";")) if s]
        assert len(statements) == 1, (
            f"the genesis shim must stay ONE statement, found {len(statements)}: {statements}"
        )

    def test_the_shim_only_touches_license_tenant_id(self):
        stmt = SHIM.read_text(encoding="utf-8").lower()
        assert "alter table license" in stmt
        assert "tenant_id" in stmt
