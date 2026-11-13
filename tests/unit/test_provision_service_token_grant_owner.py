"""``provision_service_token.py --grant-owner``: the service tenant is granted to another app.

WHY: ``svc-agent-api`` (tenant ``9a4366bf-…``, owner ``platform``) was registered by
this script with no grant step, so w2v's tts-backend got 403 ``tenant_not_accessible``
from ``GET /licenses/active`` on it (migration 073 repaired prod). These tests assert
the outcome the grantee depends on: an ACTIVE ``tenant_app_grant`` written through the
same helper as ``PUT /tenants/{id}/grants/{owner_key}``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.conftest import TEST_DATABASE_URL

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "provision_service_token.py"
_spec = importlib.util.spec_from_file_location("provision_service_token_go", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
pst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pst)

SUBJECT = "svc-grant-owner-probe"
TENANT_ID = pst._default_tenant_id(SUBJECT)


def _run(monkeypatch, *extra: str) -> int:
    # CI's ephemeral postgres:18 serves no TLS; DatabaseManager defaults to sslmode=require.
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


def _grants(db_manager) -> set[tuple[str, str]]:
    return {
        (str(g.grantee_owner_key), str(g.granted_by))
        for g in db_manager.list_tenant_grants(TENANT_ID)
    }


class TestGrantOwner:
    def test_mint_path_registers_and_grants(self, db_manager, seed_app, seed_plan, monkeypatch):
        assert _run(monkeypatch, "--grant-owner", "w2v") == 0
        assert db_manager.get_tenant(TENANT_ID).owner_key == "platform"
        assert _grants(db_manager) == {("w2v", pst.GRANTED_BY)}
        assert db_manager.owner_can_access_tenant("w2v", TENANT_ID)

    def test_register_only_grants_too_and_is_repeatable(
        self, db_manager, seed_app, seed_plan, monkeypatch
    ):
        args = ("--register-only", "--grant-owner", "w2v", "--grant-owner", "fr")
        assert _run(monkeypatch, *args) == 0
        assert _run(monkeypatch, *args) == 0  # idempotent: no second active row
        assert _grants(db_manager) == {("w2v", pst.GRANTED_BY), ("fr", pst.GRANTED_BY)}
        assert db_manager.list_licenses(tenant_id=TENANT_ID, include_deleted=True) == []

    def test_existing_grant_is_left_alone(self, db_manager, seed_app, seed_plan, monkeypatch):
        assert _run(monkeypatch, "--register-only") == 0
        db_manager.put_tenant_grant(TENANT_ID, "w2v", "operator")
        assert _run(monkeypatch, "--register-only", "--grant-owner", "w2v") == 0
        assert _grants(db_manager) == {("w2v", "operator")}

    def test_the_tenant_owner_is_skipped(self, db_manager, seed_app, seed_plan, monkeypatch):
        assert _run(monkeypatch, "--register-only", "--grant-owner", "platform") == 0
        assert _grants(db_manager) == set()

    def test_unknown_owner_exits_2_and_writes_nothing(
        self, db_manager, seed_app, seed_plan, monkeypatch
    ):
        assert _run(monkeypatch, "--grant-owner", "no-such-owner") == 2
        assert db_manager.get_tenant(TENANT_ID) is None
        assert db_manager.get_subject_by_keycloak_sub(SUBJECT) is None

    def test_rerun_on_existing_licence_grants(self, db_manager, seed_app, seed_plan, monkeypatch):
        # The svc-agent-api shape: licence already minted, grant added on a later run.
        assert _run(monkeypatch) == 0
        assert _run(monkeypatch, "--grant-owner", "w2v") == 0
        assert _grants(db_manager) == {("w2v", pst.GRANTED_BY)}

    def test_exit_3_refusal_writes_no_grant(self, db_manager, seed_app, seed_plan, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
        other = ["--subject", "svc-grant-owner-other", "--tenant-id", TENANT_ID]
        common = ["--app-key", "test-app", "--plan", "trial", "--tenant-owner", "platform"]
        assert pst.main([*other, *common]) == 0
        assert _run(monkeypatch, "--grant-owner", "w2v") == 3
        assert _grants(db_manager) == set()

    def test_no_flag_grants_nothing(self, db_manager, seed_app, seed_plan, monkeypatch):
        assert _run(monkeypatch, "--register-only") == 0
        assert _grants(db_manager) == set()
