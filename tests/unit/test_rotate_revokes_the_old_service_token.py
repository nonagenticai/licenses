"""``provision_service_token.py --rotate`` must KILL the credential it replaces.

THE DEFECT
----------
The script's own contract says ``--rotate`` is for "when the raw token was lost or
must be rotated". Its implementation was an unconditional
``create_token(..., expires_at=None)``, and the word "revoke" did not appear
anywhere in the file. So a rotation minted a SECOND live token and left the first
one exactly as it was: still unrevoked, still ``expires_at IS NULL``, still
introspecting ``active`` under the same ``subject_id``.

That ``subject_id`` is the on-behalf-of ALLOWLIST KEY
(``GRAPH_ADAPTER_ONBEHALF_SUBJECTS`` / ``KAG_GRAPH_ONBEHALF_SUBJECTS``), so the
holder of a leaked token kept the right to assert an arbitrary ``X-Tenant-Id``
against graph_adapter and kag_graph — indefinitely, because no clock was ever set
to close it, and the one action an operator would take to close it did nothing.

These tests assert the OUTCOME (the old raw token no longer introspects active),
not that some particular method was called: the whole point is that the old
credential stops working.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from uuid import UUID

import pytest

from models import Token
from tests.conftest import TEST_DATABASE_URL

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "provision_service_token.py"
_spec = importlib.util.spec_from_file_location("provision_service_token", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
provision_service_token = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_service_token)

SUBJECT = "svc-rotate-probe"
_TOKEN_LINE = re.compile(r"^token\s*:\s*(\S+)$", re.MULTILINE)


def _run(capsys, monkeypatch, *extra: str) -> tuple[int, str]:
    # The conftest connects through a raw ``create_engine`` (no SSL), but the
    # script builds its own ``DatabaseManager``, which appends
    # ``sslmode=require`` by default (models.py ``_append_ssl_params``, env
    # ``POSTGRES_SSLMODE``). CI's per-job ephemeral ``postgres:18`` serves no
    # TLS, so without this the connection fails with "server does not support
    # SSL, but SSL was required". Same idiom as ``test_provision_w2v_tenant``.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    rc = provision_service_token.main(
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
    return rc, capsys.readouterr().out


def _token_from(out: str) -> str:
    match = _TOKEN_LINE.search(out)
    assert match is not None, f"no token line in output:\n{out}"
    return match.group(1)


@pytest.fixture
def provisioned(db_manager, seed_app, seed_plan, capsys, monkeypatch) -> str:
    """Mint the first service token and hand back its RAW value."""
    rc, out = _run(capsys, monkeypatch)
    assert rc == 0, out
    return _token_from(out)


class TestRotateRevokesThePredecessor:
    def test_old_token_no_longer_introspects_active(
        self, db_manager, provisioned, capsys, monkeypatch
    ):
        """The leaked-credential property: after --rotate the OLD raw token is dead."""
        assert db_manager.introspect_token(provisioned)["active"] is True  # precondition

        rc, out = _run(capsys, monkeypatch, "--rotate")
        assert rc == 0, out
        new_token = _token_from(out)
        assert new_token != provisioned

        old = db_manager.introspect_token(provisioned)
        assert old["active"] is False
        assert old["reason"] == "token_invalid"  # revoked, not merely absent
        assert db_manager.introspect_token(new_token)["active"] is True

    def test_the_old_row_is_marked_revoked_with_a_timestamp(
        self, db_manager, provisioned, capsys, monkeypatch
    ):
        """Revocation is an administrative fact on the row, not an inference."""
        rc, out = _run(capsys, monkeypatch, "--rotate")
        assert rc == 0, out

        session = db_manager.get_session()
        try:
            row = (
                session.query(Token).filter(Token.token_hash == Token.hash_token(provisioned)).one()
            )
            assert row.revoked is True
            assert row.revoked_at is not None
        finally:
            session.close()

    def test_rotation_leaves_exactly_one_live_token_on_the_licence(
        self, db_manager, provisioned, capsys, monkeypatch
    ):
        """Two rotations must not accumulate three live credentials."""
        for _ in range(2):
            rc, out = _run(capsys, monkeypatch, "--rotate")
            assert rc == 0, out
        latest = _token_from(out)

        introspected = db_manager.introspect_token(latest)
        assert introspected["active"] is True
        license_id = UUID(str(introspected["license_id"]))

        session = db_manager.get_session()
        try:
            live = (
                session.query(Token)
                .filter(Token.license_id == license_id, Token.revoked.is_(False))
                .all()
            )
            assert len(live) == 1
            assert live[0].token_hash == Token.hash_token(latest)
        finally:
            session.close()

    def test_replacement_is_still_perpetual(self, db_manager, provisioned, capsys, monkeypatch):
        """Rotation inherits the predecessor's expiry — for a service token, none.

        Pins that closing the revocation hole did not quietly change the credential's
        lifetime contract: these are long-lived in-cluster identities.
        """
        rc, out = _run(capsys, monkeypatch, "--rotate")
        assert rc == 0, out
        session = db_manager.get_session()
        try:
            row = (
                session.query(Token)
                .filter(Token.token_hash == Token.hash_token(_token_from(out)))
                .one()
            )
            assert row.expires_at is None
        finally:
            session.close()

    def test_reports_how_many_credentials_it_killed(
        self, db_manager, provisioned, capsys, monkeypatch
    ):
        """The operator must be able to SEE that something was revoked."""
        rc, out = _run(capsys, monkeypatch, "--rotate")
        assert rc == 0
        assert "revoked previous: 1" in out


class TestWithoutRotateNothingIsDestroyed:
    def test_plain_rerun_is_still_a_no_op(self, db_manager, provisioned, capsys, monkeypatch):
        """Idempotent re-provisioning must NOT revoke — only --rotate rotates."""
        rc, out = _run(capsys, monkeypatch)
        assert rc == 0
        assert "unchanged" in out
        assert db_manager.introspect_token(provisioned)["active"] is True


class TestThereIsNoOptOut:
    def test_no_keep_old_style_flag_exists(self):
        """A flag that skips the revoke would restore the exact defect this closes."""
        with pytest.raises(SystemExit):
            provision_service_token._parse_args(
                ["--subject", SUBJECT, "--keep-old", "--tenant-owner", "platform"]
            )
