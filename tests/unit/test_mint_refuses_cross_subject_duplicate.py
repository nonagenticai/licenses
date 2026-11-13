"""``provision_service_token.py`` must refuse to mint a DUPLICATE (tenant, app) pair.

THE DEFECT
----------
The DB's uniqueness guard ``uq_license_active_tenant_app_subject`` keys on
**subject**, and that is correct: fr/kanban/n8n are sold per-tenant, so a second
employee onboarded in one org legitimately adds a second active row for the same
(tenant, app). ``get_active_license`` documents exactly that.

But billing does not key that way. ``billing_service`` builds its attribution map
on ``(tenant_id, app_name.casefold())`` and DROPS any key with more than one
candidate rather than picking one — deliberately, because picking would invent an
invoice line. So a second row makes BOTH licences unresolvable: ``license_id``
goes NULL, ``resolve_app`` then returns None, ``app_id`` goes NULL, and every
event for that tenant and app becomes unattributable spend.

The subject-keyed index is therefore SILENT for precisely the write that breaks
billing: this script minting under a fresh *service* subject into a tenant that a
*human* subject already holds a licence for. That is not hypothetical — licence
``01a06374`` was minted onto the ``fr-test-user`` tenant at 2026-09-02 18:50:03Z
and re-broke attribution that had been repaired 2.5 hours earlier.

These tests assert the OUTCOME an operator cares about — that the second mint
writes NOTHING — rather than that some particular guard function was called.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

from models import License
from tests.conftest import TEST_DATABASE_URL

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "provision_service_token.py"
_spec = importlib.util.spec_from_file_location("provision_service_token", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
provision_service_token = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_service_token)

SHARED_TENANT = str(uuid.uuid4())


def _run(capsys, monkeypatch, subject: str, *extra: str) -> tuple[int, str, str]:
    # Same idiom as test_rotate_revokes_the_old_service_token: the script builds
    # its own DatabaseManager, which appends sslmode=require by default, and CI's
    # per-job ephemeral postgres:18 serves no TLS.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    rc = provision_service_token.main(
        [
            "--subject",
            subject,
            "--app-key",
            "test-app",
            "--plan",
            "trial",
            "--tenant-owner",
            "platform",
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _active_licences_for(db_manager, tenant_id: str) -> list[License]:
    session = db_manager.get_session()
    try:
        return (
            session.query(License)
            .filter(License.tenant_id == tenant_id, License.status == "active")
            .all()
        )
    finally:
        session.close()


class TestCrossSubjectDuplicateIsRefused:
    def test_second_subject_into_the_same_tenant_exits_3_and_writes_nothing(
        self, db_manager, seed_app, seed_plan, capsys, monkeypatch
    ):
        """The 01a06374 incident, reproduced: same tenant + app, a NEW subject."""
        rc, out, _ = _run(capsys, monkeypatch, "svc-dup-first", "--tenant-id", SHARED_TENANT)
        assert rc == 0, out
        before = _active_licences_for(db_manager, SHARED_TENANT)
        assert len(before) == 1

        rc, _, err = _run(capsys, monkeypatch, "svc-dup-second", "--tenant-id", SHARED_TENANT)

        assert rc == 3, f"expected refusal, got {rc}; stderr:\n{err}"
        after = _active_licences_for(db_manager, SHARED_TENANT)
        assert len(after) == 1, "a licence was written despite the refusal"
        assert {str(lic.id) for lic in after} == {str(lic.id) for lic in before}

    def test_the_refusal_names_the_licence_that_blocks_it(
        self, db_manager, seed_app, seed_plan, capsys, monkeypatch
    ):
        """An operator must be able to act on the message without a DB query."""
        rc, out, _ = _run(capsys, monkeypatch, "svc-dup-first", "--tenant-id", SHARED_TENANT)
        assert rc == 0, out
        blocking = _active_licences_for(db_manager, SHARED_TENANT)[0]

        rc, _, err = _run(capsys, monkeypatch, "svc-dup-second", "--tenant-id", SHARED_TENANT)

        assert rc == 3
        assert str(blocking.id) in err
        assert "--allow-shared-tenant" in err

    def test_allow_shared_tenant_is_a_real_override(
        self, db_manager, seed_app, seed_plan, capsys, monkeypatch
    ):
        """A second user in one org is legitimate — the escape hatch must work."""
        rc, out, _ = _run(capsys, monkeypatch, "svc-dup-first", "--tenant-id", SHARED_TENANT)
        assert rc == 0, out

        rc, out, err = _run(
            capsys,
            monkeypatch,
            "svc-dup-second",
            "--tenant-id",
            SHARED_TENANT,
            "--allow-shared-tenant",
        )

        assert rc == 0, err
        assert len(_active_licences_for(db_manager, SHARED_TENANT)) == 2


class TestTheGuardDoesNotFireWhereItMustNot:
    def test_rerunning_the_same_subject_is_still_an_idempotent_noop(
        self, db_manager, seed_app, seed_plan, capsys, monkeypatch
    ):
        """The guard must not break the documented idempotency contract.

        Same subject means ``find_active_license`` matches, so no row is minted
        and there is no duplicate to refuse. Exit 0, not 3.
        """
        rc, out, _ = _run(capsys, monkeypatch, "svc-dup-first", "--tenant-id", SHARED_TENANT)
        assert rc == 0, out

        rc, out, err = _run(capsys, monkeypatch, "svc-dup-first", "--tenant-id", SHARED_TENANT)

        assert rc == 0, err
        assert len(_active_licences_for(db_manager, SHARED_TENANT)) == 1

    def test_the_implicit_uuid5_tenant_path_is_unguarded(
        self, db_manager, seed_app, seed_plan, capsys, monkeypatch
    ):
        """Without --tenant-id each service gets its OWN deterministic tenant.

        Two services can never collide there, so the guard must stay out of the
        way — this is the path almost every real invocation takes.
        """
        rc_a, out_a, err_a = _run(capsys, monkeypatch, "svc-solo-alpha")
        rc_b, out_b, err_b = _run(capsys, monkeypatch, "svc-solo-beta")

        assert rc_a == 0, err_a
        assert rc_b == 0, err_b
        tenant_a = str(uuid.uuid5(provision_service_token._SERVICE_TENANT_NS, "svc-solo-alpha"))
        tenant_b = str(uuid.uuid5(provision_service_token._SERVICE_TENANT_NS, "svc-solo-beta"))
        assert tenant_a != tenant_b
        assert len(_active_licences_for(db_manager, tenant_a)) == 1
        assert len(_active_licences_for(db_manager, tenant_b)) == 1


@pytest.mark.parametrize("flag", ["--allow-shared-tenant"])
def test_the_override_flag_is_documented(flag):
    """A silent override is how the next session re-creates the same incident."""
    source = _SCRIPT_PATH.read_text()
    assert flag in source
    assert "exit 3" in source, "the refusal contract must be in the module docstring"
