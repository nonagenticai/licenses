"""``provision_service_token.py --register-only`` registers the service tenant and mints NOTHING.

WHY (commonpractice#431)
------------------------
commonpractice's ``ensurePlatformInference`` mints the platform's inference-router
licence itself, under ``cp-tenant:<tenant>:a``, on the licences tenant named by
``INFERENCE_PLATFORM_TENANT_ID``. The licences mint guard refuses an unregistered
tenant, so that tenant has to exist first -- and the only tool that created it,
this script, ALSO minted an inference-router licence under ``svc-commonpractice``.
Two active (tenant, app) licences under two subjects is the pair billing's
license_resolver drops, so every platform cost event would land with
``license_id`` NULL.

These tests assert the OUTCOME the consumer depends on: after ``--register-only``
the tenant exists as kind='service' and holds no licence, so the consumer's own
mint is the ONLY active licence for the app on that tenant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest

from tests.conftest import TEST_DATABASE_URL

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "provision_service_token.py"
_spec = importlib.util.spec_from_file_location("provision_service_token_ro", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
pst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pst)

SUBJECT = "svc-register-only-probe"
TENANT_ID = pst._default_tenant_id(SUBJECT)


def _run(monkeypatch, *extra: str) -> int:
    # Same SSL idiom as test_rotate_revokes_the_old_service_token: CI's ephemeral
    # postgres:18 serves no TLS and DatabaseManager defaults to sslmode=require.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    return pst.main(
        [
            "--subject",
            SUBJECT,
            "--app-key",
            "test-app",
            "--plan",
            "trial",
            "--tenant-owner",
            "platform",
            *extra,
        ]
    )


def _licences(db_manager) -> list:
    return db_manager.list_licenses(tenant_id=TENANT_ID, include_deleted=True)


def test_default_tenant_for_svc_commonpractice_is_the_script_namespace_not_url():
    """The id to put in INFERENCE_PLATFORM_TENANT_ID. #431 quoted the NAMESPACE_URL
    uuid5 (b2583f0d-...), which this script never produces.

    Both literals were computed OUTSIDE this module (python3 -c 'uuid.uuid5(...)'),
    so a change to the script's namespace or derivation reddens this.
    """
    assert pst._default_tenant_id("svc-commonpractice") == "b8e94a81-dbd3-5f75-8a58-45deceaef000"
    assert pst._default_tenant_id("svc-commonpractice") != "b2583f0d-0fcd-5453-a7c3-e2a91defd001"


def _consumer_mint(db_manager, seed_app, seed_plan):
    """What commonpractice's ensurePlatformInference does: mint under cp-tenant:<tid>:a."""
    cp_subject = db_manager.create_or_get_subject(
        keycloak_sub=f"cp-tenant:{TENANT_ID}:a", keycloak_realm="cp-tenant"
    )
    return db_manager.create_license(
        subject_id=UUID(str(cp_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=TENANT_ID,
        status="active",
    )


class TestRegisterOnly:
    def test_registers_a_service_tenant_and_mints_nothing(
        self, db_manager, seed_app, seed_plan, monkeypatch
    ):
        assert _run(monkeypatch, "--register-only") == 0

        tenant = db_manager.get_tenant(TENANT_ID)
        assert tenant is not None
        assert str(tenant.name) == SUBJECT
        assert str(tenant.kind) == "service"
        assert str(tenant.byo_mode) == "local_allowed"
        assert _licences(db_manager) == []
        # No subject either: the consumer mints under its own subject.
        assert db_manager.get_subject_by_keycloak_sub(SUBJECT) is None

    def test_is_idempotent(self, db_manager, seed_app, seed_plan, monkeypatch):
        assert _run(monkeypatch, "--register-only") == 0
        assert _run(monkeypatch, "--register-only") == 0
        assert _licences(db_manager) == []

    def test_rerun_after_the_consumer_minted_is_a_no_op(
        self, db_manager, seed_app, seed_plan, monkeypatch, capsys
    ):
        """CP has minted cp-tenant:<tid>:a; re-running --register-only must exit 0, write nothing."""
        assert _run(monkeypatch, "--register-only") == 0
        consumer = _consumer_mint(db_manager, seed_app, seed_plan)
        capsys.readouterr()

        assert _run(monkeypatch, "--register-only") == 0
        assert str(consumer.id) in capsys.readouterr().out
        assert [str(lic.id) for lic in _licences(db_manager)] == [str(consumer.id)]

    def test_plain_run_refuses_to_mint_beside_the_consumer_licence(
        self, db_manager, seed_app, seed_plan, monkeypatch
    ):
        """The derived uuid5 tenant is NOT private once a consumer minted on it: a later
        run WITHOUT --tenant-id must not add a second active (tenant, app) licence."""
        assert _run(monkeypatch, "--register-only") == 0
        consumer = _consumer_mint(db_manager, seed_app, seed_plan)

        assert _run(monkeypatch) == 3
        assert [str(lic.id) for lic in _licences(db_manager)] == [str(consumer.id)]

    def test_consumer_mint_is_then_the_only_active_licence(
        self, db_manager, seed_app, seed_plan, monkeypatch
    ):
        """The property billing needs: exactly ONE active (tenant, app) licence."""
        assert _run(monkeypatch, "--register-only") == 0
        _consumer_mint(db_manager, seed_app, seed_plan)
        assert len(db_manager.get_active_license(TENANT_ID, "test-app")) == 1

    def test_refuses_when_the_tenant_already_holds_an_active_licence_for_the_app(
        self, db_manager, seed_app, seed_plan, monkeypatch, capsys
    ):
        """An earlier run WITHOUT the flag minted one; a consumer mint would duplicate it."""
        assert _run(monkeypatch) == 0
        before = _licences(db_manager)
        assert len(before) == 1
        capsys.readouterr()

        assert _run(monkeypatch, "--register-only") == 3
        err = capsys.readouterr().err
        assert str(before[0].id) in err
        assert len(_licences(db_manager)) == 1  # nothing written, nothing revoked

    @pytest.mark.parametrize("flag", ["--rotate", "--allow-shared-tenant"])
    def test_refuses_mint_only_flags(self, db_manager, seed_app, seed_plan, monkeypatch, flag):
        assert _run(monkeypatch, "--register-only", flag) == 2
        assert db_manager.get_tenant(TENANT_ID) is None

    def test_without_the_flag_the_script_still_mints(
        self, db_manager, seed_app, seed_plan, monkeypatch
    ):
        """Control: the default path is unchanged (one licence on the service tenant)."""
        assert _run(monkeypatch) == 0
        assert len(db_manager.get_active_license(TENANT_ID, "test-app")) == 1
