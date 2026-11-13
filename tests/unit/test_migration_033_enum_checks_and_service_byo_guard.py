"""Migration 033 — three unconstrained-enum gaps, closed together.

WHAT WAS WRONG, MEASURED.

1. `license.status` is NOT NULL but had no CHECK. `effective_status()`'s own
   docstring says an unrecognised stored value is returned VERBATIM rather than
   normalised — a design that only matters if a bad value can land in the column
   at all. Measured on prod 2026-09-07: every one of 258 rows holds `active`
   (164), `revoked` (92) or `expired` (2). The column comment (models.py:1477)
   and `tests/unit/test_effective_status_contract.py::TestStoredPendingIsTheIntendedImprovement`
   both name a FOURTH value, `pending`, as an intended (if not yet written)
   stored state — so the vocabulary here is those four, not the three prod
   currently holds. Excluding `pending` would foreclose a documented direction
   AND break `test_models.py::TestLicenseModel::test_license_is_active_status_logic`,
   which persists it today to pin `is_active()`'s behaviour.

2. `token.token_type` is NOT NULL, no CHECK. Measured on prod 2026-09-07: `api`
   (176) and `internal` (74) — zero `node` rows. But `node` is NOT an unexpected
   value to exclude: `main.TokenType` (a `StrEnum`) declares it, and
   `POST /licenses/{id}/tokens?token_type=node` is a LIVE, reachable route that
   persists it (main.py `create_license_token` -> `token_type.value` ->
   `db_manager.create_token`). Zero rows today just means nobody has minted one
   yet. Constraining to `('api', 'internal')` would 500 the very first `node`
   mint — a self-inflicted version of the outage this migration exists to
   prevent elsewhere.

3. Migration 031's header proposed `CHECK(NOT(kind='service' AND
   byo_mode='byo_required'))` and declined to add it because
   `scripts/provision_service_token.py`'s insert path could not set `byo_mode`.
   That ONE call site was fixed in #183 (`ensure_tenant` now takes a `byo_mode`
   parameter and the script passes `byo_mode="local_allowed"`) — but
   `ensure_tenant` itself did not couple the two: `kind="service"` alone, with
   no explicit `byo_mode`, still fell through to the column's own
   `server_default('byo_required')`. Measured directly against an empty schema
   here (2026-09-07): `ensure_tenant(uuid4(), name=..., kind="service")` — no
   `byo_mode` — persists `byo_mode='byo_required'`. Several EXISTING test call
   sites do exactly this (`test_tenant_rename_and_archive.py`,
   `test_endpoints.py`), so the incoherent combination was still reachable
   through the shared primitive, just not through the one caller 031 named.
   This migration's CHECK is only safe because `ensure_tenant` is fixed
   alongside it in the same commit (see `TestEnsureTenantClosesTheGeneratorGap`
   below) — a machine-identity tenant is now BORN `local_allowed` whenever
   `kind='service'` and no `byo_mode` was given, and an explicit
   `byo_mode='byo_required'` for `kind='service'` is refused with
   `ClientSafeError` before it ever reaches the constraint.

WHY THIS FILE COVERS BOTH THE DB CONSTRAINT AND THE ORM/APPLICATION GUARD.
`tests/unit/test_schema_parity.py` already proves ORM <-> migration agreement
for the whole schema; it is not repeated here. What IS here: that the
constraint actually rejects a bad value written directly (bypassing any
application-level guard), and that the application-level default/guard in
`ensure_tenant` is what keeps every existing caller of the shared primitive
inside the constraint's boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from models import ClientSafeError, Tenant

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "033_enum_checks_and_service_byo_guard.sql"
DOWN = REPO / "migrations" / "downgrades" / "033_enum_checks_and_service_byo_guard.sql"


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _down() -> str:
    return DOWN.read_text(encoding="utf-8")


class TestLicenseStatusCheck:
    def test_an_unrecognised_status_is_rejected(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        with pytest.raises(IntegrityError):
            db_manager.create_license(
                subject_id=UUID(str(seed_subject.id)),
                app_id=UUID(str(seed_app.id)),
                plan_id=UUID(str(seed_plan.id)),
                tenant_id="01000000-0000-7000-8000-000000000001",
                status="bogus-status",
            )

    @pytest.mark.parametrize("status", ["active", "revoked", "expired", "pending"])
    def test_the_four_documented_values_are_all_still_writable(
        self, db_manager, seed_subject, seed_app, seed_plan, status
    ):
        """`pending` is documented (models.py:1477) as an intended stored value —
        not yet used in production, but not something this migration may foreclose
        (see module docstring, point 1)."""
        lic = db_manager.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id="01000000-0000-7000-8000-000000000001",
            status=status,
        )
        assert lic.status == status


class TestTokenTypeCheck:
    def test_an_unrecognised_token_type_is_rejected(self, db_manager, seed_license):
        with pytest.raises(IntegrityError):
            # An explicit expiry gets past the new-token expiry policy (licenses#244),
            # which would otherwise refuse an unknown type before the DB CHECK sees it.
            db_manager.create_token(
                license_id=UUID(str(seed_license.id)),
                token_type="bogus",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )

    @pytest.mark.parametrize("token_type", ["api", "node", "internal"])
    def test_the_three_enum_values_are_all_still_writable(
        self, db_manager, seed_license, token_type
    ):
        """`node` has 0 live rows (measured 2026-09-07) but is a reachable value
        via `POST /licenses/{id}/tokens?token_type=node` (main.TokenType) — see
        module docstring, point 2."""
        token, _raw = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type=token_type,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        assert token.token_type == token_type


class TestTenantsServiceByoGuard:
    def test_the_db_constraint_rejects_the_bad_combination_directly(self, db_manager):
        """Bypasses `ensure_tenant` entirely — a raw insert is what proves the
        CONSTRAINT does the rejecting, not just the Python-level guard."""
        session = db_manager.get_session()
        try:
            bad = Tenant(
                id=uuid4(),
                name="svc-raw-bad-combo",
                kind="service",
                byo_mode="byo_required",
                owner_key="platform",
            )
            session.add(bad)
            with pytest.raises(IntegrityError):
                session.commit()
        finally:
            session.close()

    def test_every_other_kind_byo_mode_combination_still_writes(self, db_manager):
        """The guard is scoped to exactly one (kind, byo_mode) pair — every other
        combination, including 'service' with a non-byo_required mode, and
        'byo_required' with a non-service kind, must be untouched."""
        for kind, byo_mode in [
            ("service", "local_allowed"),
            ("service", "platform_only"),
            ("customer", "byo_required"),
            ("internal", "byo_required"),
            ("test", "byo_required"),
            ("unclassified", "byo_required"),
        ]:
            slug = byo_mode.replace("_", "-")
            t = db_manager.ensure_tenant(
                uuid4(),
                name=f"combo-{kind}-{slug}",
                kind=kind,
                byo_mode=byo_mode,
                owner_key="platform",
            )
            assert t.kind == kind
            assert t.byo_mode == byo_mode


class TestEnsureTenantClosesTheGeneratorGap:
    """`ensure_tenant` is the shared primitive every provisioning path and several
    tests call directly. The CHECK above is only safe if THIS closes the gap for
    every caller, not just `provision_service_token.py` (already fixed in #183).
    """

    def test_a_service_tenant_with_no_byo_mode_is_born_local_allowed(self, db_manager):
        """Measured on unmodified code: this call persisted byo_mode='byo_required'
        (the column server_default), reproducing the exact incoherent state
        migration 031 repaired. It must now default to 'local_allowed'."""
        t = db_manager.ensure_tenant(
            uuid4(), name="svc-defaulted", kind="service", owner_key="platform"
        )
        assert t.byo_mode == "local_allowed"

    def test_an_explicit_byo_required_for_a_service_tenant_is_refused(self, db_manager):
        """A caller who explicitly asks for the incoherent combination gets a
        named ClientSafeError, not an opaque IntegrityError out of the new
        constraint — matching `set_tenant_byo_mode`'s existing behaviour."""
        with pytest.raises(ClientSafeError, match="byo_required"):
            db_manager.ensure_tenant(
                uuid4(),
                name="svc-explicit-bad",
                kind="service",
                byo_mode="byo_required",
                owner_key="platform",
            )

    def test_no_row_is_created_when_the_explicit_combination_is_refused(self, db_manager):
        tid = uuid4()
        with pytest.raises(ClientSafeError):
            db_manager.ensure_tenant(
                tid,
                name="svc-never-created",
                kind="service",
                byo_mode="byo_required",
                owner_key="platform",
            )
        assert db_manager.get_tenant(str(tid)) is None

    def test_a_non_service_tenant_is_unaffected(self, db_manager):
        """The service-only coupling must not leak to other kinds: a non-service
        tenant gets the COLUMN default ('byo_required' since migration 058,
        licenses#273) and may still be registered 'byo_required' explicitly — which
        is refused only for kind='service'."""
        t = db_manager.ensure_tenant(uuid4(), name="commercial-unaffected", owner_key="platform")
        assert t.byo_mode == "byo_required"
        explicit = db_manager.ensure_tenant(
            uuid4(), name="commercial-explicit", byo_mode="byo_required", owner_key="platform"
        )
        assert explicit.byo_mode == "byo_required"


class TestProvisionServiceTokenStillPassesItExplicitly:
    """#183 already made the script pass byo_mode explicitly; this migration does
    not depend on that alone (ensure_tenant's own default now covers every
    caller), but the explicit pass-through must not have regressed."""

    def test_the_call_site_still_names_both_kind_and_byo_mode(self):
        src = (REPO / "scripts" / "provision_service_token.py").read_text(encoding="utf-8")
        call = src.split("db.ensure_tenant(", 1)[1].split(")", 1)[0]
        assert 'kind="service"' in call
        assert 'byo_mode="local_allowed"' in call


class TestTheMigrationSQLIsGuardedAndSafe:
    """Mirrors migration 021's own safety properties: a failed migration raises
    out of `_run_db_bootstrap` and the pod REFUSES TO SERVE (replicas: 2, ext_authz
    backing store), so every ADD CONSTRAINT here must be preceded by a check that
    turns a real violation into a NAMED error, not a bare constraint failure."""

    def test_all_three_constraints_are_added(self):
        up = _up()
        assert "ck_license_status" in up
        assert "ck_token_token_type" in up
        assert "ck_tenants_service_not_byo_required" in up

    def test_each_add_is_guarded_by_a_pg_constraint_probe(self):
        up = _up()
        assert up.count("pg_constraint") >= 3

    def test_bad_rows_are_measured_before_the_add(self):
        """Same shape as 021: count violating rows and RAISE EXCEPTION naming
        them, rather than letting ADD CONSTRAINT itself fail bare."""
        up = _up()
        assert "RAISE EXCEPTION" in up

    def test_the_downgrade_drops_all_three(self):
        down = _down()
        for name in (
            "ck_license_status",
            "ck_token_token_type",
            "ck_tenants_service_not_byo_required",
        ):
            assert name in down
            assert "DROP CONSTRAINT" in down
