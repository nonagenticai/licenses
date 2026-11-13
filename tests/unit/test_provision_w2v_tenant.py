"""End-to-end tests for the w2v tenant provisioning CLI (`--byo-mode` posture).

Exercises the real ``main(argv)`` against the test DB fixture to prove the
``--byo-mode`` flag sets the tenant's BYO posture on BOTH provisioning paths:

- first-provision (license minted), and
- re-provision (idempotent skip return before the mint) — the path where a
  stale ``byo_required`` default would otherwise stick forever.

The posture-set is wired right after ``ensure_tenant`` and BEFORE the skip
return precisely so the re-provision case is covered by one call.
"""

from __future__ import annotations

import importlib.util
import os
from uuid import UUID

import pytest

from models import DatabaseManager

# Load scripts/provision_w2v_tenant.py by path — `scripts/` is not a package.
_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "provision_w2v_tenant.py",
)
_spec = importlib.util.spec_from_file_location("provision_w2v_tenant", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
provision_w2v_tenant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_w2v_tenant)

_TENANT_ID = "01000000-0000-7000-8000-0000000000aa"
_TENANT_NAME = "w2v-byo-test"


def _seed_w2v_app_and_plan(db_manager: DatabaseManager) -> None:
    """Seed the `w2v`/`standard` app+plan the script resolves before minting."""
    app = db_manager.create_app(
        key="w2v", name="w2v", description="w2v package", issuers=("platform",)
    )
    db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")


def _argv(*, byo_mode: str | None) -> list[str]:
    argv = [
        "--tenant-id",
        _TENANT_ID,
        "--tenant-name",
        _TENANT_NAME,
        "--owner-sub",
        "owner-keycloak-sub",
        "--tenant-owner",
        "platform",
    ]
    if byo_mode is not None:
        argv += ["--byo-mode", byo_mode]
    return argv


@pytest.fixture
def _db_url(db_manager: DatabaseManager, monkeypatch) -> None:
    """Point the script's own DatabaseManager at the test DB via DATABASE_URL.

    The conftest connects through a raw ``create_engine`` (no SSL), but the
    script builds its own ``DatabaseManager`` which force-appends
    ``sslmode=require`` (models.py) — and the CI/local test Postgres has no SSL.
    Disable the forced SSL for the script's connection so it reaches the test DB.
    """
    url = db_manager.engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")


def test_byo_mode_set_on_first_provision(db_manager: DatabaseManager, _db_url) -> None:
    _seed_w2v_app_and_plan(db_manager)

    rc = provision_w2v_tenant.main(_argv(byo_mode="local_allowed"))

    assert rc == 0
    tenant = db_manager.get_tenant(_TENANT_ID)
    assert tenant is not None and tenant.byo_mode == "local_allowed"


def test_byo_mode_set_on_reprovision_skip_path(db_manager: DatabaseManager, _db_url) -> None:
    # First provision mints the license and sets local_allowed.
    _seed_w2v_app_and_plan(db_manager)
    assert provision_w2v_tenant.main(_argv(byo_mode="local_allowed")) == 0

    # Second run hits the idempotent-skip return (license already active) BEFORE
    # the mint — but the posture-set runs earlier, so a corrected posture still
    # lands. This is the regression the ordering fix guards.
    rc = provision_w2v_tenant.main(_argv(byo_mode="platform_only"))

    assert rc == 0
    tenant = db_manager.get_tenant(_TENANT_ID)
    assert tenant is not None and tenant.byo_mode == "platform_only"


def test_no_byo_mode_flag_leaves_default(db_manager: DatabaseManager, _db_url) -> None:
    # Without --byo-mode the script must not touch the posture: the tenant keeps
    # the DB default ('byo_required' since migration 058, licenses#273).
    _seed_w2v_app_and_plan(db_manager)

    assert provision_w2v_tenant.main(_argv(byo_mode=None)) == 0
    tenant = db_manager.get_tenant(_TENANT_ID)
    assert tenant is not None and tenant.byo_mode == "byo_required"
